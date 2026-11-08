"""2d: load MeSH descriptors from the brain-and-mind branches (MESH_BRANCHES) as OntologyTerm nodes.

Source: NLM ASCII descriptor file d<year>.bin (public, free; NLM Memorandum of Understanding). Downloaded once to
data/raw/ when missing. Each descriptor keeps its preferred name (MH) and entry terms (ENTRY, PRINT ENTRY) as synonyms;
an inverted name ("Memory, Short-Term") also gets its natural order ("Short-Term Memory").
kind = the first MESH_BRANCHES prefix that matches one of the descriptor's tree numbers (dict order is the priority).
id = MESH:<UI>, the same ids Hetionet uses for its Symptom nodes: those nodes keep kind 'Symptom' and gain MeSH synonyms.
One concept, one node: a MeSH disease that a DOID term cross-references (DOID xref MESH:<UI>) is not loaded as its own
node; its names are added to that DOID term's synonyms, so NED links both vocabularies' names to the DOID id.
Run after the doid and hetionet stages. Writes results/mesh_profile.json.
"""
import datetime
import json
import re
from collections import Counter
import requests
from config import CONTACT_EMAIL, DATA, MESH_BRANCHES, RESULTS

URL = "https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/asciimesh/d{year}.bin"


def download():
    """Newest d<year>.bin already in data/raw, else download next year's or this year's file from NLM."""
    have = sorted((DATA / "raw").glob("d20[0-9][0-9].bin"))
    if have:
        return have[-1]
    year = datetime.date.today().year
    head = {"User-Agent": f"KE_PROJECT capstone (mailto:{CONTACT_EMAIL})"}
    for y in (year + 1, year):
        r = requests.get(URL.format(year=y), headers=head, timeout=300)
        if r.status_code == 200:
            path = DATA / "raw" / f"d{y}.bin"
            path.write_bytes(r.content)
            print(f"downloaded MeSH {y} -> {path}")
            return path
    raise SystemExit(f"no MeSH descriptor file found at {URL.format(year=year)}; download it to data/raw/ by hand")


def uninvert(name):
    """'Memory, Short-Term' -> 'Short-Term Memory'; names without exactly one ', ' are returned unchanged."""
    parts = name.split(", ")
    return f"{parts[1]} {parts[0]}" if len(parts) == 2 else name


def parse(text, branches):
    """ASCII MeSH text -> (rows, Counter of kinds). Descriptors outside every branch are skipped."""
    rows, kinds = [], Counter()
    for rec in text.split("*NEWRECORD")[1:]:
        ui = re.search(r"^UI = (\S+)", rec, re.M)
        name = re.search(r"^MH = (.+)$", rec, re.M)
        if not (ui and name):
            continue
        trees = re.findall(r"^MN = (\S+)", rec, re.M)
        kind = next((k for prefix, k in branches.items() if any(t.startswith(prefix) for t in trees)), None)
        if kind is None:
            continue
        name = name.group(1).strip()
        entries = [e.split("|")[0].strip() for e in re.findall(r"^(?:PRINT ENTRY|ENTRY) = (.+)$", rec, re.M)]
        syn = sorted({s for e in entries + [name] for s in (e, uninvert(e))} - {name})
        rows.append({"id": f"MESH:{ui.group(1)}", "name": name, "kind": kind, "trees": trees,
                     "synonyms": syn, "synonyms_text": " | ".join(syn)})
        kinds[kind] += 1
    return rows, kinds


def merge_synonyms(existing_name, existing_syn, new_names):
    """Existing synonyms plus new names, deduplicated case-insensitively, never repeating the node's own name."""
    out, seen = [], {(existing_name or "").lower()}
    for n in list(existing_syn or []) + list(new_names):
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


if __name__ == "__main__":
    from config import NEO4J_DB
    from core.db import driver, run_batches
    path = download()
    rows, _ = parse(path.read_text(encoding="utf-8", errors="replace"), MESH_BRANCHES)
    q = lambda cypher, **kw: driver.execute_query(cypher, database_=NEO4J_DB, **kw)[0]
    to_doid = {r["mesh"]: r["doid"] for r in q("""MATCH (o:OntologyTerm {source: 'DOID'}) UNWIND o.xrefs AS x
        WITH o, x WHERE x STARTS WITH 'MESH:' RETURN x AS mesh, o.id AS doid""")}
    # existing nodes these rows touch: Hetionet Symptom nodes (same MESH: ids) and DOID terms (by cross-reference)
    target = {r["id"]: (to_doid[r["id"]] if r["kind"] == "Disease" and r["id"] in to_doid else r["id"]) for r in rows}
    have = {r["id"]: r for r in q("MATCH (o:OntologyTerm) WHERE o.id IN $ids RETURN o.id AS id, o.name AS name, o.synonyms AS syn",
                                  ids=sorted(set(target.values())))}
    new, update = [], {}
    for r in rows:
        tid = target[r["id"]]
        if tid in have:                           # add MeSH names to the existing node; keep its name, kind and source
            node = update.setdefault(tid, {"id": tid, "name": have[tid]["name"], "synonyms": have[tid]["syn"] or [],
                                           "trees": [], "merged_doid": tid != r["id"]})
            node["synonyms"] = merge_synonyms(node["name"], node["synonyms"], [r["name"]] + r["synonyms"])
            node["trees"] += r["trees"]
        else:
            new.append(r)
    for u in update.values():
        u["synonyms_text"] = " | ".join(u["synonyms"])
    run_batches("""
    UNWIND $rows AS r
    MERGE (o:OntologyTerm {id: r.id})
    SET o.name = r.name, o.name_lc = toLower(r.name), o.kind = r.kind, o.source = 'MeSH', o.synonyms = r.synonyms,
        o.synonyms_text = r.synonyms_text, o.mesh_trees = r.trees, o.in_mesh = true, o.mesh_version = $version
    """, new, version=path.stem)
    run_batches("""
    UNWIND $rows AS r
    MATCH (o:OntologyTerm {id: r.id})
    SET o.synonyms = r.synonyms, o.synonyms_text = r.synonyms_text, o.mesh_trees = r.trees, o.in_mesh = true,
        o.mesh_version = $version
    """, list(update.values()), version=path.stem)
    profile = {"file": path.name, "descriptors_in_branches": len(rows), "new_terms": len(new),
               "new_by_kind": dict(Counter(r["kind"] for r in new)),
               "merged_into_doid": sum(u["merged_doid"] for u in update.values()),
               "merged_into_hetionet_symptom": sum(not u["merged_doid"] for u in update.values()), "branches": MESH_BRANCHES}
    json.dump(profile, open(RESULTS / "mesh_profile.json", "w"), indent=2)
    print(profile)