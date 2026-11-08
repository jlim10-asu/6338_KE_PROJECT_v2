import json
from rdflib import Graph, Namespace, OWL, RDF, RDFS
from config import DOID_OWL, RESULTS
from core.db import run_batches

OIO = Namespace("http://www.geneontology.org/formats/oboInOwl#")
DOID = "http://purl.obolibrary.org/obo/DOID_"

g = Graph()
g.parse(DOID_OWL, format="xml")
version = next((str(g.value(o, OWL.versionIRI)) for o in g.subjects(RDF.type, OWL.Ontology)), None)

rows, obsolete = [], 0
for s in g.subjects(RDF.type, OWL.Class):
    if not str(s).startswith(DOID):
        continue
    if any(str(o).lower() == "true" for o in g.objects(s, OWL.deprecated)):
        obsolete += 1
        continue
    label = g.value(s, RDFS.label)
    if label is None:
        continue
    syn = sorted({str(o) for p in (OIO.hasExactSynonym, OIO.hasRelatedSynonym) for o in g.objects(s, p)})
    rows.append({
        "id": "DOID:" + str(s).rsplit("_", 1)[1], "uri": str(s), "name": str(label),
        "synonyms": syn, "synonyms_text": " | ".join(syn),
        "xrefs": sorted(str(o) for o in g.objects(s, OIO.hasDbXref)),
    })

run_batches("""
UNWIND $rows AS r
MERGE (o:OntologyTerm {id: r.id})
SET o.uri = r.uri, o.name = r.name, o.name_lc = toLower(r.name), o.synonyms = r.synonyms,
    o.synonyms_text = r.synonyms_text, o.xrefs = r.xrefs, o.source = 'DOID', o.kind = 'Disease', o.source_version = $version
""", rows, version=version)
profile = {"doid_version": version, "triples": len(g), "classes_loaded": len(rows), "obsolete_skipped": obsolete,
           "with_synonyms": sum(1 for r in rows if r["synonyms"])}
json.dump(profile, open(RESULTS / "doid_profile.json", "w"), indent=2)
print(profile)
