"""GraphRAG engine (FR-5 to FR-8). Imported by ask.py and eval/eval_qa.py; not a pipeline stage.

  route()     LLM picks a question type + Cypher template and extracts parameters (it never writes Cypher)
  retrieve()  vector indexes (stage 8a) give seed nodes; the template runs read-only with a timeout (FR-7)
  answer()    generator writes cited sentences from the rows only; code checks every cited ID (FR-8)
  mode="baseline": top-k statements and claims by vector similarity, no graph traversal (comparison for FR-5)
"""
import json
import time
from neo4j import READ_ACCESS, unit_of_work
from pydantic import ValidationError
from config import COMM_EXCLUDE_ENTITIES, NEO4J_DB
from core.db import driver
from core.embed import qa_embed
from core.llm import llm_json, LLMStop
from s8_qa.prompts import (ROUTER_SYSTEM, ROUTER_USER, ROUTER_REPAIR, ROUTER_VERSION,
                           GENERATOR_SYSTEM, GENERATOR_USER, GENERATOR_REPAIR, GENERATOR_VERSION)
from s8_qa.schemas import RouterDecision, RouterParams, GeneratorOutput

ROUTER_MODEL, ROUTER_EFFORT = "claude-haiku-5-5", "low"
GENERATOR_MODEL, GENERATOR_EFFORT = "claude-sonnet-5-5", "low"
QUERY_TIMEOUT_S = 10                       # FR-7: every template runs read-only with this time limit
K_TEXT, K_ENTITY, K_COMMUNITY, K_BASELINE = 5, 3, 5, 8
MIN_COS_TEXT, MIN_COS_ENTITY, MIN_COS_COMMUNITY = 0.30, 0.60, 0.25
MAX_PAPERS, MIN_RELEASES, RANK_LIMIT = 10, 3, 10
EXCLUDE = [e.lower() for e in COMM_EXCLUDE_ENTITIES]

# ---------------- Cypher templates (fixed text; only parameters vary) ----------------
STATEMENT_CHECK = """
UNWIND $seeds AS seed
MATCH (s:Statement {statement_id: seed.id})-[:APPEARS_IN]->(r:SourceRelease)-[:ANNOUNCES]->(p:Paper)
OPTIONAL MATCH (r)-[:ISSUED_BY]->(i:Institution)
OPTIONAL MATCH (s)-[g:GROUNDS_IN]->(c:ScientificClaim)
RETURN seed.cos AS seed_cos, s.statement_id AS statement_id, s.evidence_span AS press_text,
       s.resolution_status AS resolution_status, s.unresolved_reason AS unresolved_reason,
       r.euid AS euid, toString(r.release_date) AS release_date, i.name AS institution,
       p.doi AS doi, p.title AS paper_title, p.is_retracted AS paper_retracted,
       c.claim_id AS claim_id, c.evidence_span AS paper_text,
       g.statement_strength AS press_strength, g.claim_strength AS paper_strength, g.exaggeration_label AS label,
       g.direction_conflict AS direction_conflict, g.null_reframed AS null_reframed, g.explanation AS explanation
ORDER BY seed_cos DESC"""

CLAIM_CHECK = """
UNWIND $seeds AS seed
MATCH (p:Paper)-[:REPORTS]->(c:ScientificClaim {claim_id: seed.id})
RETURN seed.cos AS seed_cos, c.claim_id AS claim_id, c.evidence_span AS paper_text, c.direction AS direction,
       c.claim_strength AS paper_strength, c.population AS population,
       p.doi AS doi, p.title AS paper_title, p.journal AS journal, p.is_retracted AS paper_retracted,
       [(s:Statement)-[g:GROUNDS_IN]->(c) | {statement_id: s.statement_id, euid: s.euid, press_text: s.evidence_span,
         press_strength: g.statement_strength, label: g.exaggeration_label}] AS press
ORDER BY seed_cos DESC"""

RELEASE_CHECK = """
MATCH (s:Statement)-[:APPEARS_IN]->(r:SourceRelease)-[:ANNOUNCES]->(p:Paper) WHERE r.euid IN $euids
OPTIONAL MATCH (s)-[g:GROUNDS_IN]->(c:ScientificClaim)
RETURN r.euid AS euid, p.doi AS doi, p.title AS paper_title, p.is_retracted AS paper_retracted,
       s.statement_id AS statement_id, s.evidence_span AS press_text, s.resolution_status AS resolution_status,
       s.unresolved_reason AS unresolved_reason, c.claim_id AS claim_id, c.evidence_span AS paper_text,
       g.statement_strength AS press_strength, g.claim_strength AS paper_strength, g.exaggeration_label AS label,
       g.explanation AS explanation
ORDER BY euid, statement_id"""

# FR-6: claim -> shared entity (same role, exact links only; anchors are not used) -> other claim -> other paper
CROSS_PAPER = """
MATCH (p0:Paper)-[:REPORTS]->(c:ScientificClaim) WHERE c.claim_id IN $start_claims
MATCH (c)-[e1:HAS_EXPOSURE|HAS_OUTCOME]->(m:MedicalEntity)<-[e2:HAS_EXPOSURE|HAS_OUTCOME]-(c2:ScientificClaim)<-[:REPORTS]-(p2:Paper)
WHERE type(e1) = type(e2) AND p2 <> p0 AND NOT coalesce(p2.is_retracted, false) AND NOT toLower(m.canonical_name) IN $exclude
WITH c, p2, c2, collect(DISTINCT type(e1)) AS roles, collect(DISTINCT m.canonical_name) AS shared
ORDER BY size(roles) DESC, c2.claim_id
WITH c, p2, collect({claim_id: c2.claim_id, paper_text: c2.evidence_span, direction: c2.direction,
     paper_strength: c2.claim_strength, shared: shared,
     match_type: CASE size(roles) WHEN 2 THEN 'same_exposure_and_outcome' ELSE 'same_' + toLower(substring(roles[0], 4)) END,
     same_direction: CASE WHEN size(roles) = 2 THEN c.direction = c2.direction END})[..3] AS claims
RETURN c.claim_id AS start_claim_id, c.direction AS start_direction, p2.doi AS doi, p2.title AS paper_title, claims
ORDER BY size([x IN claims WHERE x.match_type = 'same_exposure_and_outcome']) DESC, size(claims) DESC, doi
LIMIT $max_papers"""

_COUNTS = """count(g) AS scored,
       sum(CASE g.exaggeration_label WHEN 'overstated' THEN 1 ELSE 0 END) AS overstated,
       sum(CASE g.exaggeration_label WHEN 'aligned' THEN 1 ELSE 0 END) AS aligned,
       sum(CASE g.exaggeration_label WHEN 'understated' THEN 1 ELSE 0 END) AS understated,
       collect(DISTINCT CASE WHEN g.exaggeration_label = 'overstated' THEN r.euid END)[..5] AS overstated_euids,
       collect(DISTINCT r.euid)[..5] AS example_euids"""
_YEARS = "($year_from IS NULL OR r.release_date.year >= $year_from) AND ($year_to IS NULL OR r.release_date.year <= $year_to)"
_RATE = "CASE WHEN scored = 0 THEN null ELSE round(toFloat(overstated) / scored, 3) END AS overstated_rate"

AGG_RATE_BY_YEAR = f"""
MATCH (r:SourceRelease) WHERE {_YEARS}
OPTIONAL MATCH (r)<-[:APPEARS_IN]-(s:Statement)
OPTIONAL MATCH (s)-[g:GROUNDS_IN]->(:ScientificClaim)
WITH r.release_date.year AS year, count(DISTINCT r) AS releases, {_COUNTS}
RETURN year, releases, scored, overstated, aligned, understated, {_RATE}, overstated_euids, example_euids ORDER BY year"""

AGG_INSTITUTION_RANK = f"""
MATCH (i:Institution)<-[:ISSUED_BY]-(r:SourceRelease)<-[:APPEARS_IN]-(s:Statement) WHERE {_YEARS}
OPTIONAL MATCH (s)-[g:GROUNDS_IN]->(:ScientificClaim)
WITH i, count(DISTINCT r) AS releases, {_COUNTS}
WHERE releases >= $min_releases AND scored > 0
RETURN i.name AS institution, releases, scored, overstated, aligned, understated, {_RATE}, overstated_euids, example_euids
ORDER BY overstated_rate DESC, scored DESC LIMIT $limit"""

AGG_BY_ENTITY = f"""
MATCH (m:MedicalEntity)<-[e:HAS_EXPOSURE|HAS_OUTCOME]-(s:Statement)-[:APPEARS_IN]->(r:SourceRelease)
WHERE m.canonical_name IN $entities AND {_YEARS}
OPTIONAL MATCH (s)-[g:GROUNDS_IN]->(:ScientificClaim)
WITH m, CASE type(e) WHEN 'HAS_EXPOSURE' THEN 'exposure' ELSE 'outcome' END AS role, count(DISTINCT r) AS releases, {_COUNTS}
RETURN m.canonical_name AS entity, role, releases, scored, overstated, aligned, understated, {_RATE}, overstated_euids, example_euids
ORDER BY releases DESC"""

TOPIC_OVERVIEW = """
UNWIND $seeds AS seed
MATCH (k:Community {community_id: seed.id})
RETURN seed.cos AS seed_cos, k.community_id AS community_id, k.title AS title, k.summary AS summary,
       k.key_findings AS key_findings, k.n_claims AS n_claims, k.n_papers AS n_papers, k.press_labels AS press_labels,
       [(k)<-[:IN_COMMUNITY]-(c:ScientificClaim)<-[:REPORTS]-(p:Paper) WHERE c.claim_id IN coalesce(k.key_claim_ids, [])
        | {claim_id: c.claim_id, doi: p.doi}] AS key_claims
ORDER BY seed_cos DESC"""

BASELINE = {
    "vector_statements": """CALL db.index.vector.queryNodes('statement_embedding', $k, $v) YIELD node, score
        RETURN round(2 * score - 1, 3) AS seed_cos, node.statement_id AS statement_id, node.euid AS euid, node.evidence_span AS press_text""",
    "vector_claims": """CALL db.index.vector.queryNodes('claim_embedding', $k, $v) YIELD node, score
        RETURN round(2 * score - 1, 3) AS seed_cos, node.claim_id AS claim_id, node.doi AS doi, node.evidence_span AS paper_text""",
}

INDEXES = {"statement_embedding": ("statement_id", "euid"), "claim_embedding": ("claim_id", "doi"),
           "entity_embedding": ("canonical_name", None), "community_embedding": ("community_id", None)}


# ---------------- Database helpers ----------------
@unit_of_work(timeout=QUERY_TIMEOUT_S)
def _read_tx(tx, query, params):
    return tx.run(query, params).data()


def read(query, **params):
    """Read-only transaction with a timeout: Neo4j rejects writes in a READ session (FR-7)."""
    with driver.session(database=NEO4J_DB or None, default_access_mode=READ_ACCESS) as session:
        return session.execute_read(_read_tx, query, params)


def vector_seeds(index, text, k, min_cos, filter_value=None):
    """Nearest nodes in a vector index -> [{id, cos}]. Neo4j scores are (1 + cos) / 2, converted back to cosine."""
    key, prop = INDEXES[index]
    where = f"AND node.{prop} = $fv" if filter_value and prop else ""
    return read(f"""CALL db.index.vector.queryNodes($index, $n, $v) YIELD node, score
        WITH node, 2 * score - 1 AS cos WHERE cos >= $min_cos {where}
        RETURN node.{key} AS id, round(cos, 3) AS cos ORDER BY cos DESC LIMIT $k""",
                index=index, n=200 if where else k, v=qa_embed([text])[0], min_cos=min_cos, fv=filter_value, k=k)


# ---------------- Router ----------------
def route(question, use_cache=True):
    user = prompt = ROUTER_USER.format(question=question)
    for attempt in range(2):
        try:
            raw, cached = llm_json(RouterDecision, ROUTER_SYSTEM, prompt, tag=f"router{attempt}", version=ROUTER_VERSION,
                                   effort=ROUTER_EFFORT, use_cache=use_cache, model=ROUTER_MODEL)
            return RouterDecision.model_validate_json(raw), cached
        except ValidationError as e:
            prompt = ROUTER_REPAIR.format(user=user, error=" ".join(str(e).split())[:600])
        except LLMStop:
            return RouterDecision(reasoning="refused", path_type="unsupported", template_id=None, params=RouterParams()), False
    # two invalid outputs: fall back to a statement search on the question text
    return RouterDecision(reasoning="fallback", path_type="local", template_id="statement_check", params=RouterParams()), False


# ---------------- Retrieval ----------------
def run_template(tid, p: RouterParams, question):
    text = p.text or question
    years = {"year_from": p.year_from, "year_to": p.year_to}
    if tid == "statement_check":
        return {tid: read(STATEMENT_CHECK, seeds=vector_seeds("statement_embedding", text, K_TEXT, MIN_COS_TEXT, p.euid))}
    if tid == "claim_check":
        return {tid: read(CLAIM_CHECK, seeds=vector_seeds("claim_embedding", text, K_TEXT, MIN_COS_TEXT, p.doi))}
    if tid == "release_check":
        if p.euid:
            euids = [p.euid]
        elif p.doi:
            euids = [r["euid"] for r in read("MATCH (r:SourceRelease)-[:ANNOUNCES]->(:Paper {doi: $doi}) RETURN r.euid AS euid", doi=p.doi)]
        else:
            top = vector_seeds("statement_embedding", text, 1, MIN_COS_TEXT)
            euids = [read("MATCH (s:Statement {statement_id: $id}) RETURN s.euid AS euid", id=top[0]["id"])[0]["euid"]] if top else []
        return {tid: read(RELEASE_CHECK, euids=euids)}
    if tid == "cross_paper":
        local = read(STATEMENT_CHECK, seeds=vector_seeds("statement_embedding", text, 1, MIN_COS_TEXT, p.euid))
        start = sorted({r["claim_id"] for r in local if r["claim_id"]}) or \
            [s["id"] for s in vector_seeds("claim_embedding", text, 1, MIN_COS_TEXT, p.doi)]
        rows = read(CROSS_PAPER, start_claims=start, exclude=EXCLUDE, max_papers=MAX_PAPERS) if start else []
        return {"statement_check": local, tid: rows}
    if tid == "agg_rate_by_year":
        return {tid: read(AGG_RATE_BY_YEAR, **years)}
    if tid == "agg_institution_rank":
        return {tid: read(AGG_INSTITUTION_RANK, min_releases=p.min_releases or MIN_RELEASES, limit=RANK_LIMIT, **years)}
    if tid == "agg_by_entity":
        names = {s["id"] for ph in (p.entities or [text]) for s in vector_seeds("entity_embedding", ph, K_ENTITY, MIN_COS_ENTITY)}
        return {tid: read(AGG_BY_ENTITY, entities=sorted(names), **years)}
    if tid == "topic_overview":
        seeds = vector_seeds("community_embedding", p.text or ", ".join(p.entities) or question, K_COMMUNITY, MIN_COS_COMMUNITY)
        rows = read(TOPIC_OVERVIEW, seeds=seeds)
        for r in rows:  # stored as JSON text by stage 7c
            r["key_findings"] = json.loads(r["key_findings"] or "[]")
            r["press_labels"] = json.loads(r["press_labels"] or "{}")
        return {tid: rows}
    raise ValueError(f"unknown template {tid}")


def context_ids(results):
    """DOIs and release IDs present anywhere in the rows: the only IDs an answer may cite (FR-8)."""
    ids = set()

    def walk(x, key=None):
        if isinstance(x, dict):
            for k, v in x.items():
                walk(v, k)
        elif isinstance(x, list):
            for v in x:
                walk(v, key)
        elif x is not None and key == "doi":
            ids.add(f"doi:{str(x).lower()}")
        elif x is not None and key in ("euid", "overstated_euids", "example_euids"):
            ids.add(f"release:{x}")
    walk(results)
    return ids


def retrieve(question, mode="graph", use_cache=True):
    t0 = time.perf_counter()
    if mode == "baseline":
        decision, cached = RouterDecision(reasoning="baseline", path_type="local", template_id="statement_check",
                                          params=RouterParams()), None
        t1 = time.perf_counter()
        v = qa_embed([question])[0]
        results = {name: read(q, k=K_BASELINE, v=v) for name, q in BASELINE.items()}
    else:
        decision, cached = route(question, use_cache)
        t1 = time.perf_counter()
        results = {} if decision.template_id is None else run_template(decision.template_id, decision.params, question)
    t2 = time.perf_counter()
    return {"route": decision.model_dump(), "router_cached": cached, "results": results,
            "timing_ms": {"route": round((t1 - t0) * 1000), "retrieval": round((t2 - t1) * 1000)}}


# ---------------- Generator and citation check ----------------
def render_rows(results):
    out = []
    for tid, rows in results.items():
        out.append(f"## {tid} ({len(rows)} rows)")
        out += [json.dumps({k: v for k, v in r.items() if v not in (None, [])}, default=str, ensure_ascii=False) for r in rows]
    return "\n".join(out)


def norm_cite(c):
    kind, _, value = c.strip().strip("[]()").partition(":")
    kind, value = kind.strip().lower(), value.strip()
    return f"doi:{value.lower()}" if kind == "doi" else f"release:{value}" if kind in ("release", "euid") else c.lower()


def flag_citations(sentences, allowed):
    """FR-8: finding sentences need >= 1 citation, and every cited ID must be in the retrieved rows."""
    flagged = []
    for i, s in enumerate(sentences):
        bad = [c for c in s["citations"] if norm_cite(c) not in allowed]
        if bad or (s["kind"] == "finding" and not s["citations"]):
            flagged.append({"sentence": i, "text": s["text"], "unknown_ids": bad})
    return flagged


def generate(question, ret, allowed, use_cache=True):
    r = ret["route"]
    user = prompt = GENERATOR_USER.format(question=question, path_type=r["path_type"], template_id=r["template_id"],
                                          rows=render_rows(ret["results"]))
    sentences, insufficient, flagged, cached = None, True, [], None
    for attempt in range(2):
        try:
            raw, c = llm_json(GeneratorOutput, GENERATOR_SYSTEM, prompt, tag=f"generator{attempt}", version=GENERATOR_VERSION,
                              effort=GENERATOR_EFFORT, use_cache=use_cache, model=GENERATOR_MODEL)
        except LLMStop:
            break
        out = GeneratorOutput.model_validate_json(raw)
        cached = c if cached is None else cached
        sentences, insufficient = [s.model_dump() for s in out.sentences], out.insufficient_context
        flagged = flag_citations(sentences, allowed)
        if not flagged:
            break
        prompt = GENERATOR_REPAIR.format(user=user, error="; ".join(f"sentence {f['sentence'] + 1}: {f['unknown_ids'] or 'no citation'}"
                                                                    for f in flagged))
    return sentences, insufficient, flagged, cached


def answer(question, mode="graph", use_cache=True):
    ret = retrieve(question, mode, use_cache)
    allowed = context_ids(ret["results"])
    t0 = time.perf_counter()
    sentences, insufficient, flagged, cached = None, True, [], None
    if ret["route"]["template_id"] is None:
        msg = "This system answers questions about brain-and-mind press releases and their papers; it gives no medical advice."
    elif not any(ret["results"].values()):
        msg = "No matching records were found in the graph for this question."
    else:
        msg = "The answer could not be generated."
        sentences, insufficient, flagged, cached = generate(question, ret, allowed, use_cache)
    sentences = sentences or [{"text": msg, "kind": "limitation", "citations": []}]
    t = {**ret["timing_ms"], "generate": round((time.perf_counter() - t0) * 1000)}
    t["end_to_end"] = t["route"] + t["retrieval"] + t["generate"]
    tid = ret["route"]["template_id"]
    return {"question": question, "mode": mode,
            "answer": " ".join(s["text"] + (f" [{', '.join(s['citations'])}]" if s["citations"] else "") for s in sentences),
            "sentences": sentences, "flagged": flagged, "insufficient_context": insufficient,
            "path_type": ret["route"]["path_type"], "template_id": tid,
            "table": ret["results"].get(tid) if ret["route"]["path_type"] == "aggregate" else None,  # FR-7 tabular output
            "uncached": ret["router_cached"] is not True and cached is False,
            "timing_ms": t, "retrieval": ret, "allowed_ids": sorted(allowed)}
