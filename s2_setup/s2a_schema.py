"""Neo4j constraints, lookup indexes and the NED full-text index."""
from config import ROOT
from core.db import run_cypher_file

for r in run_cypher_file(ROOT / "s2_setup" / "cypher" / "schema.cypher"):
    print("OK", r["query"][:90])
