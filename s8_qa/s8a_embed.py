"""Stage 8a: embed node text and (re)create the Neo4j vector indexes the GraphRAG engine seeds from.

  entity_embedding     MedicalEntity    canonical_name + aliases
  claim_embedding      ScientificClaim  text
  statement_embedding  Statement        text
  community_embedding  Community        title + summary (from 7c)
Model: core.embed.QA_EMBED_MODEL (MedEmbed-base-v0.1), the same one the engine embeds questions with. Re-run after load or analysis.
Output: results/embedding_summary.json
"""
import json
from config import NEO4J_DB, RESULTS
from core.db import driver, run_batches
from core.embed import QA_EMBED_MODEL, qa_embed

INDEXES = [  # name, label, key, text expression
    ("entity_embedding", "MedicalEntity", "canonical_name",
     "n.canonical_name + reduce(s = '', a IN coalesce(n.aliases, []) | s + '; ' + a)"),
    ("claim_embedding", "ScientificClaim", "claim_id", "coalesce(n.text, n.evidence_span)"),
    ("statement_embedding", "Statement", "statement_id", "coalesce(n.text, n.evidence_span)"),
    ("community_embedding", "Community", "community_id", "CASE WHEN n.summary IS NULL THEN NULL ELSE coalesce(n.title, '') + '. ' + n.summary END"),
]


def q(query, **params):
    return [r.data() for r in driver.execute_query(query, database_=NEO4J_DB, **params).records]


summary = {"model": QA_EMBED_MODEL, "indexes": {}}
for name, label, key, text in INDEXES:
    q(f"DROP INDEX {name} IF EXISTS")
    rows = [r for r in q(f"MATCH (n:{label}) RETURN n.{key} AS id, {text} AS text") if r["id"] is not None and (r["text"] or "").strip()]
    if not rows:
        summary["indexes"][name] = 0
        continue
    vecs = qa_embed(r["text"] for r in rows)
    run_batches(f"""UNWIND $rows AS r MATCH (n:{label} {{{key}: r.id}})
        CALL db.create.setNodeVectorProperty(n, 'embedding', r.vec)""", [{"id": r["id"], "vec": v} for r, v in zip(rows, vecs)])
    q(f"""CREATE VECTOR INDEX {name} IF NOT EXISTS FOR (n:{label}) ON n.embedding
        OPTIONS {{indexConfig: {{`vector.dimensions`: {len(vecs[0])}, `vector.similarity_function`: 'cosine'}}}}""")
    summary["indexes"][name] = len(rows)
q("CALL db.awaitIndexes(600)")

json.dump(summary, open(RESULTS / "embedding_summary.json", "w"), indent=2)
print(json.dumps(summary, indent=2))
