"""NED: link each distinct exposure / outcome mention to one OntologyTerm (DOID or Hetionet), or leave it unlinked.

v6 routes by entity type (set by the extract stage on every triple):
  - each mention gets one semantic_type: the majority of its triples' object_type (ties: order of schemas.ENTITY_TYPES)
  - NED_KINDS_BY_TYPE (config.py) lists the ontology kinds that type may link to; exact-lexicon matches and full-text
    candidates are restricted to those kinds
  - a type with no kinds (procedure, physiology, behavior, other: the loaded vocabularies do not cover them) is recorded as
    unlinked with method not_in_vocab and no LLM call
Writes the ned table (ontology_kind = the linked term's kind; semantic_type and type_votes = the extracted type) and
results/ned_summary.json.
"""
import json
from collections import Counter, defaultdict
from config import LLM_MODEL, NED_CANDIDATES, NED_KINDS_BY_TYPE, NEO4J_DB, RESULTS
from core.db import db, driver
from core.llm import LLMStop, llm_json
from core.text import lucene_escape, norm
from s4_extract.prompts import NED_SYSTEM, NED_USER, NED_VERSION
from core.types import majority_type
from s4_extract.schemas import NedDecision

KINDS_BY_TYPE = NED_KINDS_BY_TYPE

db.execute("""CREATE TABLE IF NOT EXISTS ned (mention_key TEXT PRIMARY KEY, mention TEXT, ontology_id TEXT,
  canonical_name TEXT, ontology_kind TEXT, confidence REAL, method TEXT, model_id TEXT, semantic_type TEXT, type_votes TEXT)""")
done = {r[0] for r in db.execute("SELECT mention_key FROM ned")}

terms, _, _ = driver.execute_query("MATCH (o:OntologyTerm) RETURN o.id AS id, o.name AS name, o.kind AS kind, coalesce(o.synonyms, []) AS syn", database_=NEO4J_DB)
lexicon = defaultdict(set)
info = {}
for t in terms:
    info[t["id"]] = t
    for s in [t["name"]] + t["syn"]:
        lexicon[norm(s)].add(t["id"])

mentions, votes = {}, defaultdict(Counter)
for obj, obj_type, span in db.execute("SELECT t.object, t.object_type, f.evidence_span FROM triples t JOIN findings f USING (finding_id)"):
    key = norm(obj)
    mentions.setdefault(key, (obj.strip(), span))
    votes[key][obj_type or "other"] += 1

stats = defaultdict(int)
for key, (mention, context) in mentions.items():
    if key in done:
        continue
    stype = majority_type(votes[key])
    tv = json.dumps(dict(votes[key]))
    kinds = KINDS_BY_TYPE.get(stype, [])
    if not kinds:
        row = (key, mention, None, key, None, None, "not_in_vocab", None)
    else:
        exact = {i for i in lexicon.get(key, set()) if info[i]["kind"] in kinds}
        if len(exact) == 1:
            oid = next(iter(exact))
            row = (key, mention, oid, info[oid]["name"], info[oid]["kind"], 1.0, "exact_lexicon", None)
        else:
            q = " OR ".join(lucene_escape(w) for w in key.split() if len(w) > 2) or lucene_escape(key)
            cands, _, _ = driver.execute_query("""
                CALL db.index.fulltext.queryNodes('ontology_names', $q, {limit: $pool}) YIELD node, score
                WITH node, score WHERE node.kind IN $kinds
                RETURN node.id AS id, node.name AS name, node.kind AS kind, left(coalesce(node.synonyms_text, ''), 200) AS syn, score
                ORDER BY score DESC LIMIT $k""",
                q=q, pool=NED_CANDIDATES * 5, kinds=kinds, k=NED_CANDIDATES, database_=NEO4J_DB)
            cands = [c.data() for c in cands]
            ids = {c["id"] for c in cands}
            cands += [{"id": i, "name": info[i]["name"], "kind": info[i]["kind"], "syn": ""} for i in exact if i not in ids]
            ids = {c["id"] for c in cands}
            if not cands:
                row = (key, mention, None, key, None, 0.0, "no_candidates", None)
            else:
                lines = "\n".join(f"{c['id']} | {c['name']} | {c['kind']} | {c['syn']}" for c in cands)
                try:
                    raw, _ = llm_json(NedDecision, NED_SYSTEM, NED_USER.format(mention=mention, context=context, candidates=lines), tag="ned", version=NED_VERSION)
                    d = NedDecision.model_validate_json(raw)
                except LLMStop as e:
                    if e.reason != "refusal":  # max_tokens cut-offs stay unrecorded and retry next run
                        stats["llm_" + e.reason] += 1
                        continue
                    d = None  # refusals repeat, so record as unlinked
                if d is None:
                    row = (key, mention, None, key, None, 0.0, "llm_refused", LLM_MODEL)
                elif d.selected_id in ids:
                    row = (key, mention, d.selected_id, info[d.selected_id]["name"], info[d.selected_id]["kind"], d.confidence, "llm_select", LLM_MODEL)
                else:
                    method = "llm_nil" if d.selected_id.upper() == "NIL" else "llm_invalid_id"
                    row = (key, mention, None, key, None, d.confidence, method, LLM_MODEL)
    stats[row[6]] += 1
    db.execute("INSERT OR REPLACE INTO ned VALUES (?,?,?,?,?,?,?,?,?,?)", row + (stype, tv))
    db.commit()

summary = {r[0]: r[1] for r in db.execute("SELECT method, count(*) FROM ned GROUP BY method")}
summary["distinct_canonical"] = db.execute("SELECT count(DISTINCT canonical_name) FROM ned").fetchone()[0]
summary["distinct_mentions"] = db.execute("SELECT count(*) FROM ned").fetchone()[0]
summary["by_type"] = {t: {"mentions": n, "linked": linked} for t, n, linked in db.execute(
    "SELECT semantic_type, count(*), sum(ontology_id IS NOT NULL) FROM ned GROUP BY semantic_type ORDER BY count(*) DESC")}
summary["mixed_type_mentions"] = sum(1 for (tv,) in db.execute("SELECT type_votes FROM ned") if tv and len(json.loads(tv)) > 1)
summary["kinds_by_type"] = KINDS_BY_TYPE
json.dump(summary, open(RESULTS / "ned_summary.json", "w"), indent=2)
print(summary)
