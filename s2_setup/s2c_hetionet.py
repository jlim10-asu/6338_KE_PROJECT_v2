"""2c: Hetionet v1.0 compounds, diseases, symptoms and side effects as OntologyTerm nodes (NED candidates).

Only nodes are loaded: no stage reads Hetionet's edges (treats, palliates, presents, causes), so they are not loaded.
Needs only hetionet-v1.0-nodes.tsv.
"""
import csv
import json
from config import HETIONET_NODES, RESULTS
from core.db import run_batches

PREFIX = {"Compound": "DRUGBANK", "Disease": "", "Symptom": "MESH", "Side Effect": "UMLS"}

nodes = []
for r in csv.DictReader(open(HETIONET_NODES), delimiter="\t"):
    if r["kind"] not in PREFIX:
        continue
    raw = r["id"].split("::", 1)[1]
    cid = raw if r["kind"] == "Disease" else f"{PREFIX[r['kind']]}:{raw}"
    nodes.append({"id": cid, "name": r["name"], "kind": r["kind"], "hetionet_id": r["id"]})

run_batches("""
UNWIND $rows AS r
MERGE (o:OntologyTerm {id: r.id})
ON CREATE SET o.name = r.name, o.name_lc = toLower(r.name), o.source = 'Hetionet', o.synonyms = [], o.synonyms_text = ''
SET o.hetionet_id = r.hetionet_id, o.kind = r.kind, o.in_hetionet = true
""", nodes)

stats = {"nodes": {k: sum(1 for n in nodes if n["kind"] == k) for k in PREFIX}}
json.dump(stats, open(RESULTS / "hetionet_profile.json", "w"), indent=2)
print(stats)
