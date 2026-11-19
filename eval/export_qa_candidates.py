"""Gather source material for drafting the 20 gold questions in eval/gold/eval_queries.json. Calls no LLM.

Gold answers are written from the press text and abstracts, not from graph outputs, so the system is not graded against
itself. Aggregate truths are recomputed with pandas over SQLite, as an independent check of the Cypher templates.
  local      12 random releases: press text + abstract     (draft 8 questions)
  cross      8 paper pairs sharing an entity in one role    (draft 4)
  aggregate  counts per year, institution, top-10 entity    (draft 5)
  global     5 communities spanning 2-4 papers              (draft 3)

  python eval/export_qa_candidates.py   ->  eval/qa_candidates.json
"""
import json
import random
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from config import COMM_EXCLUDE_ENTITIES, EX_MODE, NEO4J_DB
from core.db import db, driver
from core.text import norm

OUT = Path(__file__).resolve().parent / "qa_candidates.json"
EXCLUDE = [e.lower() for e in COMM_EXCLUDE_ENTITIES]
rng = random.Random(42)


def q(query, **params):
    return [r.data() for r in driver.execute_query(query, database_=NEO4J_DB, **params).records]


rec = pd.read_sql("SELECT euid, doi, release_date, institution, paper_title FROM records", db)
chunks = pd.read_sql("SELECT source_type, source_id, text FROM chunks", db)
press = chunks[chunks.source_type == "press"].groupby("source_id").text.apply("\n\n".join).to_dict()
abstract = chunks[chunks.source_type == "abstract"].groupby("source_id").text.first().to_dict()
rec = rec[rec.euid.isin(press) & rec.doi.isin(abstract)].copy()
rec["year"] = rec.release_date.str[:4]
by_doi = rec.drop_duplicates("doi").set_index("doi")

# local
local = [{**r, "press_text": press[r["euid"]], "abstract": abstract[r["doi"]]}
         for r in rec.sample(min(12, len(rec)), random_state=42).to_dict("records")]

# cross: paper pairs sharing an entity in the same role
pairs = q("""
MATCH (p1:Paper)-[:REPORTS]->(:ScientificClaim)-[e1:HAS_EXPOSURE|HAS_OUTCOME]->(m:MedicalEntity)
      <-[e2:HAS_EXPOSURE|HAS_OUTCOME]-(:ScientificClaim)<-[:REPORTS]-(p2:Paper)
WHERE type(e1) = type(e2) AND p1.doi < p2.doi AND NOT toLower(m.canonical_name) IN $exclude
RETURN p1.doi AS doi_a, p2.doi AS doi_b, collect(DISTINCT m.canonical_name + ' (' + type(e1) + ')') AS shared""", exclude=EXCLUDE)
pairs = [p for p in pairs if p["doi_a"] in by_doi.index and p["doi_b"] in by_doi.index]
cross = [{"shared": p["shared"],
          "paper_a": {"doi": p["doi_a"], "euid": by_doi.loc[p["doi_a"]].euid, "abstract": abstract[p["doi_a"]],
                      "press_text": press[by_doi.loc[p["doi_a"]].euid]},
          "paper_b": {"doi": p["doi_b"], "euid": by_doi.loc[p["doi_b"]].euid, "abstract": abstract[p["doi_b"]]}}
         for p in rng.sample(pairs, min(8, len(pairs)))]

# aggregate: same definitions as the Cypher templates (scored = resolved statements with a label)
f = pd.read_sql("SELECT finding_id, source_id AS euid, exposure, outcome FROM findings WHERE source_type = 'press'", db)
ex = pd.read_sql("SELECT statement_id, exaggeration_label FROM exaggeration WHERE mode = ? AND resolution_status = 'resolved'",
                 db, params=(EX_MODE,))
st = f.merge(ex, left_on="finding_id", right_on="statement_id", how="left").merge(rec[["euid", "year", "institution"]], on="euid")


def counts(g):
    lab = g.exaggeration_label
    return pd.Series({"releases": g.euid.nunique(), "scored": int(lab.notna().sum()), "overstated": int((lab == "overstated").sum()),
                      "aligned": int((lab == "aligned").sum()), "understated": int((lab == "understated").sum())})


ned = dict(pd.read_sql("SELECT mention_key, canonical_name FROM ned WHERE canonical_name IS NOT NULL", db).values)
ent = pd.concat([st.assign(entity=st[c].map(lambda x: ned.get(norm(x))), role=c) for c in ("exposure", "outcome")]).dropna(subset=["entity"])
ent = ent[~ent.entity.str.lower().isin(EXCLUDE)]
top = ent.entity.value_counts().head(10).index
inst = st.groupby("institution").apply(counts)
aggregate = {"by_year": st.groupby("year").apply(counts).reset_index().to_dict("records"),
             "by_institution": inst[(inst.releases >= 3) & (inst.scored > 0)].reset_index().to_dict("records"),
             "by_entity": ent[ent.entity.isin(top)].groupby(["entity", "role"]).apply(counts).reset_index().to_dict("records")}

# global: communities spanning 2-4 papers (gold from member abstracts, not the 7c summaries)
comms = q("""MATCH (k:Community)<-[:IN_COMMUNITY]-(:ScientificClaim)<-[:REPORTS]-(p:Paper)
WITH k, collect(DISTINCT p.doi) AS dois WHERE 2 <= size(dois) <= 4
RETURN k.community_id AS community_id, k.title AS title, dois""")
glob = [{**c, "abstracts": {d: abstract.get(d) for d in c["dois"]}} for c in rng.sample(comms, min(5, len(comms)))]

json.dump({"local": local, "cross": cross, "aggregate": aggregate, "global": glob}, open(OUT, "w"), indent=1, default=str)
print(f"local {len(local)}, cross {len(cross)} (of {len(pairs)}), global {len(glob)} -> {OUT}")
