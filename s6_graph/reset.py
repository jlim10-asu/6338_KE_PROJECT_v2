"""Clear everything derived from extraction before a re-run with new prompts or a new sample.
Keeps the LLM cache, the ontology (OntologyTerm nodes from the doid / hetionet stages) and the schema. The corpus files and the
EurekAlert! database (fetched abstracts, crawled text) are not touched."""
from config import NEO4J_DB
from core.db import db, driver

for t in ("findings", "triples", "rejections", "extraction_log", "ned", "anchors", "exaggeration", "records", "chunks", "sentences"):
    db.execute(f"DROP TABLE IF EXISTS {t}")
db.commit()
driver.execute_query("""
MATCH (n) WHERE n:SourceRelease OR n:Paper OR n:Institution OR n:Chunk OR n:Statement OR n:ScientificClaim OR n:MedicalEntity OR n:Community
DETACH DELETE n""", database_=NEO4J_DB)
print("cleared SQLite derived tables and Neo4j data nodes; ontology and llm_cache kept")
