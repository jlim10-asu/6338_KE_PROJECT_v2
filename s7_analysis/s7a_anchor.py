"""7a: broader anchors for the mentions NED left unlinked (llm_nil, no_candidates). Runs first in analysis, after the
pipeline has settled every link and NIL. NED itself is unchanged.

An anchor is the most specific vocabulary term that the mention is a kind, part, property, activity or measure of
("hippocampal volume" -> Hippocampus). Anchors never merge entities: this stage writes them as MedicalEntity
-[:ANCHORED_TO]-> OntologyTerm (not DISAMBIGUATED_TO); the communities stage (7b) weights them at COMM_ANCHOR_WEIGHT;
they support browsing and reports.
Candidates: the ned stage's full-text search and kinds (NED_KINDS_BY_TYPE), minus ANCHOR_STOPLIST terms and MeSH terms
whose deepest tree number has fewer than ANCHOR_MIN_DEPTH levels (broad terms would become hubs).
Stoplist loop: add a hub term to ANCHOR_STOPLIST and rerun analysis. Rows anchored to a stoplisted term are cleared at
the start of this stage and re-anchored; every ANCHORED_TO edge is rewritten from the anchors table each run.
Writes the anchors table, ANCHORED_TO edges and results/anchor_summary.json. Mentions already anchored are skipped.
"""
import json
from collections import Counter
from config import ANCHOR_MIN_DEPTH, ANCHOR_STOPLIST, LLM_MODEL, NED_CANDIDATES, NED_KINDS_BY_TYPE, NEO4J_DB, RESULTS
from core.db import db, driver, run_batches
from core.llm import LLMStop, llm_json
from core.text import lucene_escape, norm
from s4_extract.schemas import NedDecision
from s7_analysis.prompts import ANCHOR_SYSTEM, ANCHOR_USER, ANCHOR_VERSION

db.execute("""CREATE TABLE IF NOT EXISTS anchors (mention_key TEXT PRIMARY KEY, mention TEXT, semantic_type TEXT,
  anchor_id TEXT, anchor_name TEXT, anchor_kind TEXT, confidence REAL, method TEXT, model_id TEXT)""")
stop = [s.lower() for s in ANCHOR_STOPLIST]
cleared = db.execute(f"DELETE FROM anchors WHERE lower(anchor_name) IN ({','.join('?' * len(stop))})", stop).rowcount if stop else 0
db.commit()
done = {r[0] for r in db.execute("SELECT mention_key FROM anchors")}
todo = db.execute("""SELECT mention_key, mention, semantic_type FROM ned
                     WHERE ontology_id IS NULL AND method IN ('llm_nil', 'no_candidates')""").fetchall()
context = {}
for obj, span in db.execute("SELECT t.object, f.evidence_span FROM triples t JOIN findings f USING (finding_id)"):
    context.setdefault(norm(obj), span)

stats = Counter()
for key, mention, stype in todo:
    if key in done:
        continue
    kinds = NED_KINDS_BY_TYPE.get(stype, [])
    q = " OR ".join(lucene_escape(w) for w in key.split() if len(w) > 2) or lucene_escape(key)
    cands, _, _ = driver.execute_query("""
        CALL db.index.fulltext.queryNodes('ontology_names', $q, {limit: $pool}) YIELD node, score
        WITH node, score WHERE node.kind IN $kinds AND NOT node.name_lc IN $stop
          AND (node.mesh_trees IS NULL OR any(t IN node.mesh_trees WHERE size(split(t, '.')) >= $min_depth))
        RETURN node.id AS id, node.name AS name, node.kind AS kind, left(coalesce(node.synonyms_text, ''), 200) AS syn, score
        ORDER BY score DESC LIMIT $k""",
        q=q, pool=NED_CANDIDATES * 50, kinds=kinds, stop=stop, min_depth=ANCHOR_MIN_DEPTH, k=NED_CANDIDATES,
        database_=NEO4J_DB)
    cands = [c.data() for c in cands]
    info = {c["id"]: c for c in cands}
    row = None
    if not cands:
        row = (None, None, None, 0.0, "no_candidates", None)
    else:
        lines = "\n".join(f"{c['id']} | {c['name']} | {c['kind']} | {c['syn']}" for c in cands)
        try:
            raw, _ = llm_json(NedDecision, ANCHOR_SYSTEM, ANCHOR_USER.format(mention=mention, context=context.get(key, ""),
                              candidates=lines), tag="anchor", version=ANCHOR_VERSION)
            d = NedDecision.model_validate_json(raw)
        except LLMStop as e:
            if e.reason != "refusal":            # max_tokens cut-offs stay unrecorded and retry next run
                stats["llm_" + e.reason] += 1
                continue
            d = None
        if d is None:
            row = (None, None, None, 0.0, "llm_refused", LLM_MODEL)
        elif d.selected_id in info:
            c = info[d.selected_id]
            row = (c["id"], c["name"], c["kind"], d.confidence, "anchor_select", LLM_MODEL)
        else:
            row = (None, None, None, d.confidence, "anchor_nil" if d.selected_id.upper() == "NIL" else "llm_invalid_id", LLM_MODEL)
    stats[row[4]] += 1
    db.execute("INSERT OR REPLACE INTO anchors VALUES (?,?,?,?,?,?,?,?,?)", (key, mention, stype) + row)
    db.commit()

# Graph: rewrite every ANCHORED_TO edge from the anchors table (stale edges from cleared rows go away)
driver.execute_query("MATCH ()-[a:ANCHORED_TO]->() DELETE a", database_=NEO4J_DB)
edges = [dict(zip(("entity", "anchor_id", "confidence", "method", "model_id"), r)) for r in db.execute(
    """SELECT n.canonical_name, a.anchor_id, a.confidence, a.method, a.model_id FROM anchors a
       JOIN ned n USING (mention_key) WHERE a.anchor_id IS NOT NULL""")]
run_batches("""
UNWIND $rows AS r
MATCH (m:MedicalEntity {canonical_name: r.entity}), (o:OntologyTerm {id: r.anchor_id})
MERGE (m)-[a:ANCHORED_TO]->(o)
SET a.link_type = 'broader', a.confidence = r.confidence, a.method = r.method, a.model_id = r.model_id, a.anchored_at = datetime()
""", edges)
written = driver.execute_query("MATCH ()-[a:ANCHORED_TO]->() RETURN count(a) AS n", database_=NEO4J_DB)[0][0]["n"]

summary = {"run": dict(stats), "cleared_by_stoplist": cleared, "edges_written": written,
           "by_method": dict(db.execute("SELECT method, count(*) FROM anchors GROUP BY method").fetchall()),
           "anchored_by_type": dict(db.execute("SELECT semantic_type, count(*) FROM anchors WHERE anchor_id IS NOT NULL GROUP BY 1").fetchall()),
           "anchored_by_kind": dict(db.execute("SELECT anchor_kind, count(*) FROM anchors WHERE anchor_id IS NOT NULL GROUP BY 1").fetchall()),
           "distinct_anchor_terms": db.execute("SELECT count(DISTINCT anchor_id) FROM anchors").fetchone()[0]}
json.dump(summary, open(RESULTS / "anchor_summary.json", "w"), indent=2)
print(summary)
