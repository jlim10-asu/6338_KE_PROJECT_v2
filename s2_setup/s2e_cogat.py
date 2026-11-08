"""2e: load Cognitive Atlas concepts (kind 'Mental Process') and tasks (kind 'Cognitive Task') as OntologyTerm nodes.

Source: the Cognitive Atlas REST API (free; CC BY-SA 3.0 US, so cite it in the report). Each endpoint is downloaded once
to data/raw/cogat_<kind>.json when missing. Synonyms = the comma-separated alias field.
id = COGAT:<Cognitive Atlas id> (trm_... for concepts, tsk_... for tasks). Writes results/cogat_profile.json.
"""
import json
import requests
from config import CONTACT_EMAIL, DATA, RESULTS

URL = "https://www.cognitiveatlas.org/api/v-alpha/{kind}"
KINDS = {"concept": "Mental Process", "task": "Cognitive Task"}


def download(kind):
    path = DATA / "raw" / f"cogat_{kind}.json"
    if not path.exists():
        r = requests.get(URL.format(kind=kind), headers={"User-Agent": f"KE_PROJECT capstone (mailto:{CONTACT_EMAIL})"}, timeout=300)
        r.raise_for_status()
        path.write_text(r.text, encoding="utf-8")
        print(f"downloaded Cognitive Atlas {kind}s -> {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def parse(items, kind):
    rows = []
    for c in items:
        name, cid = (c.get("name") or "").strip(), c.get("id")
        if not (name and cid):
            continue
        alias = c.get("alias") or ""
        alias = alias.split(",") if isinstance(alias, str) else alias
        syn = sorted({a.strip() for a in alias if a and a.strip()} - {name})
        rows.append({"id": f"COGAT:{cid}", "name": name, "kind": KINDS[kind], "definition": c.get("definition_text") or None,
                     "synonyms": syn, "synonyms_text": " | ".join(syn)})
    return rows


if __name__ == "__main__":
    from core.db import run_batches
    rows = [r for kind in KINDS for r in parse(download(kind), kind)]
    run_batches("""
    UNWIND $rows AS r
    MERGE (o:OntologyTerm {id: r.id})
    SET o.name = r.name, o.name_lc = toLower(r.name), o.kind = r.kind, o.source = 'Cognitive Atlas',
        o.definition = r.definition, o.synonyms = r.synonyms, o.synonyms_text = r.synonyms_text
    """, rows)
    profile = {kind: sum(1 for r in rows if r["kind"] == k) for kind, k in KINDS.items()}
    json.dump(profile, open(RESULTS / "cogat_profile.json", "w"), indent=2)
    print(profile)