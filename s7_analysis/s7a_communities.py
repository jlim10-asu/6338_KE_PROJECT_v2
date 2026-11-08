"""Community detection over ScientificClaim nodes (design schema: ScientificClaim -[:IN_COMMUNITY]-> Community).

Two claims are linked when they share a MedicalEntity via HAS_EXPOSURE or HAS_OUTCOME (weight 1.0 per shared entity), or
when their entities meet at the same OntologyTerm with at least one side a broader anchor (ANCHORED_TO, the anchor stage;
weight COMM_ANCHOR_WEIGHT per shared term; 0 turns anchors off). Anchors add structure without merging entities. Leiden (GDS) writes communityId only for communities with at least COMM_MIN_SIZE claims, so no
one-claim community exists. Community nodes are then created with plain Cypher (GDS only writes the property).
Re-runnable: the previous run's property, Community nodes, and in-memory projection are cleared first.
Community.title / summary / key_findings are left empty here; the summaries stage (7b) fills them."""
import json
from datetime import datetime, timezone
from config import NEO4J_DB, RESULTS
from core.db import driver
from config import COMM_ANCHOR_WEIGHT, COMM_GRAPH, COMM_MIN_SIZE, COMM_SEED, COMM_CROSS_PAPER_ONLY, COMM_EXCLUDE_ENTITIES


def q(query, **params):
    records, _, _ = driver.execute_query(query, database_=NEO4J_DB, **params)
    return [r.data() for r in records]


# 0. Clear the previous run
q("CREATE CONSTRAINT community_id IF NOT EXISTS FOR (k:Community) REQUIRE k.community_id IS UNIQUE")
q("CALL gds.graph.drop($g, false) YIELD graphName RETURN graphName", g=COMM_GRAPH)
q("MATCH (c:ScientificClaim) WHERE c.communityId IS NOT NULL REMOVE c.communityId")
q("MATCH (k:Community) DETACH DELETE k")

# 1. Project claim -- claim edges weighted by shared entities plus shared anchors (undirected; each pair emitted once)
proj = q("""
CALL {
  MATCH (a:ScientificClaim)-[:HAS_EXPOSURE|HAS_OUTCOME]->(m:MedicalEntity)<-[:HAS_EXPOSURE|HAS_OUTCOME]-(b:ScientificClaim)
  WHERE elementId(a) < elementId(b) AND NOT toLower(m.canonical_name) IN $exclude
  RETURN a, b, toFloat(count(DISTINCT m)) AS w
  UNION ALL
  MATCH (a:ScientificClaim)-[:HAS_EXPOSURE|HAS_OUTCOME]->(:MedicalEntity)-[r1:ANCHORED_TO|DISAMBIGUATED_TO]->(o:OntologyTerm)
        <-[r2:ANCHORED_TO|DISAMBIGUATED_TO]-(:MedicalEntity)<-[:HAS_EXPOSURE|HAS_OUTCOME]-(b:ScientificClaim)
  WHERE $anchor_w > 0 AND elementId(a) < elementId(b) AND (type(r1) = 'ANCHORED_TO' OR type(r2) = 'ANCHORED_TO')
  RETURN a, b, $anchor_w * count(DISTINCT o) AS w
}
WITH a, b, sum(w) AS shared
WHERE NOT $cross_paper_only OR NOT EXISTS { (a)<-[:REPORTS]-(:Paper)-[:REPORTS]->(b) }
WITH gds.graph.project($g, a, b,
       {relationshipProperties: {shared: toFloat(shared)}},
       {undirectedRelationshipTypes: ['*']}) AS g
RETURN g.nodeCount AS nodes, g.relationshipCount AS rels
""", g=COMM_GRAPH, cross_paper_only=COMM_CROSS_PAPER_ONLY, exclude=[e.lower() for e in COMM_EXCLUDE_ENTITIES],
   anchor_w=float(COMM_ANCHOR_WEIGHT))[0]

# 2. Leiden: writes communityId only for communities of size >= COMM_MIN_SIZE (fixed seed, single thread = reproducible)
leiden = q("""
CALL gds.leiden.write($g, {writeProperty: 'communityId', relationshipWeightProperty: 'shared',
                           minCommunitySize: $min_size, randomSeed: $seed, concurrency: 1})
YIELD communityCount, modularity, ranLevels, didConverge, nodePropertiesWritten
RETURN communityCount, modularity, ranLevels, didConverge, nodePropertiesWritten
""", g=COMM_GRAPH, min_size=COMM_MIN_SIZE, seed=COMM_SEED)[0]

# 3. Community nodes + IN_COMMUNITY edges (plain Cypher, not GDS)
run_at = datetime.now(timezone.utc).isoformat()
q("""
MATCH (c:ScientificClaim) WHERE c.communityId IS NOT NULL
WITH c.communityId AS cid, collect(c) AS members
MERGE (k:Community {community_id: cid})
SET k.size = size(members), k.algorithm = 'leiden', k.weight = 'shared_entities + anchors', k.anchor_weight = $anchor_w,
    k.min_size = $min_size, k.seed = $seed, k.cross_paper_only = $cross_paper_only,
    k.modularity = $modularity, k.created_at = datetime($run_at)
WITH k, members
UNWIND members AS c
MERGE (c)-[:IN_COMMUNITY]->(k)
""", min_size=COMM_MIN_SIZE, seed=COMM_SEED, cross_paper_only=COMM_CROSS_PAPER_ONLY,
  modularity=leiden["modularity"], run_at=run_at, anchor_w=float(COMM_ANCHOR_WEIGHT))

# 4. Size and paper spread per community (a community whose claims all come from one paper is just that paper)
comms = q("""
MATCH (k:Community)<-[:IN_COMMUNITY]-(c:ScientificClaim)<-[:REPORTS]-(p:Paper)
OPTIONAL MATCH (c)-[:HAS_EXPOSURE|HAS_OUTCOME]->(m:MedicalEntity)
OPTIONAL MATCH (m)-[:ANCHORED_TO]->(an:OntologyTerm)
WITH k, count(DISTINCT c) AS claims, count(DISTINCT p) AS papers, collect(DISTINCT m.canonical_name) AS entities,
     collect(DISTINCT an.name) AS anchors
SET k.n_papers = papers, k.anchors = anchors
RETURN k.community_id AS community_id, claims, papers, entities[..8] AS top_entities, anchors[..8] AS top_anchors
ORDER BY claims DESC
""")
total_claims = q("MATCH (c:ScientificClaim) RETURN count(c) AS n")[0]["n"]
singletons = q("MATCH (k:Community) WHERE k.size < $m RETURN count(k) AS n", m=COMM_MIN_SIZE)[0]["n"]
q("CALL gds.graph.drop($g, false) YIELD graphName RETURN graphName", g=COMM_GRAPH)

sizes = [c["claims"] for c in comms]
summary = {
    "run_at": run_at, "algorithm": "gds.leiden.write", "min_size": COMM_MIN_SIZE, "seed": COMM_SEED,
    "cross_paper_only": COMM_CROSS_PAPER_ONLY, "excluded_entities": COMM_EXCLUDE_ENTITIES, "anchor_weight": COMM_ANCHOR_WEIGHT,
    "projection": {"claims_with_shared_entity": proj["nodes"], "claim_pairs": proj["rels"]},
    "leiden": {k: leiden[k] for k in ("communityCount", "modularity", "ranLevels", "didConverge", "nodePropertiesWritten")},
    "communities_written": len(comms),
    "claims_total": total_claims,
    "claims_in_community": sum(sizes),
    "claims_unassigned": total_claims - sum(sizes),
    "size_min_max_mean": [min(sizes), max(sizes), round(sum(sizes) / len(sizes), 2)] if sizes else None,
    "multi_paper_communities": sum(1 for c in comms if c["papers"] >= 2),
    "communities_below_min_size": singletons,
    "top_communities": comms[:10],
}
json.dump(summary, open(RESULTS / "community_summary.json", "w"), indent=2, default=str)
print(json.dumps({k: v for k, v in summary.items() if k != "top_communities"}, indent=2, default=str))
