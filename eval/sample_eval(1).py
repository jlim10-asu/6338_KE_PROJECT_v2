"""PR5 held-out evaluation sheets (no LLM calls): NED links and statement matching, on the 500 run.

  python eval/sample_eval.py           -> results/ned_sheet.csv, matched_sheet.csv, nomatch_sheet.csv (blank label columns)
  python eval/sample_eval.py --score   -> reads the filled sheets, writes results/eval_sheets_summary.json

Fill the blank columns by hand or by pasting a sheet into an LLM chat with the rubric. The sheets are blind: no pipeline
confidence, explanation, method or label. --score joins them back to pipeline.db by id.
  NED sheet     N_NED linked mentions (exact_lexicon + llm_select), proportional by method
  Matched sheet   N_RES resolved statements (llm mode); No-match sheet  N_UNRES unresolved; at most one statement per release
Held out: pilot releases (corpus.csv sample_rank 1-20, plus the DEV_LIMIT=20 seed-42 draw in case the pilot used it) and
every mention seen in a pilot release or in results/ned_precision_v7.csv.
"""
import argparse
import json
import sys
from math import sqrt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from config import CORPUS_CSV, NEO4J_DB, RESULTS
from core.db import db, driver
from core.text import norm
from s3_ingest.text import load_records
from s5_reconcile.matching import label

SEED, N_NED, N_RES, N_UNRES = 42, 100, 100, 40
NED_CSV, SUMMARY = RESULTS / "ned_sheet.csv", RESULTS / "eval_sheets_summary.json"
MATCHED_CSV, NOMATCH_CSV = RESULTS / "matched_sheet.csv", RESULTS / "nomatch_sheet.csv"
SHEETS = (NED_CSV, MATCHED_CSV, NOMATCH_CSV)
YES, NO = {"y", "yes", "true", "1"}, {"n", "no", "false", "0"}


def wilson(k, n, z=1.96):
    if not n:
        return None
    p, d = k / n, 1 + z * z / n
    c, h = (p + z * z / (2 * n)) / d, z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return {"k": int(k), "n": int(n), "p": round(p, 3), "ci95": [round(c - h, 3), round(c + h, 3)]}


def yn(series):
    s = series.astype(str).str.strip().str.lower()
    return s.map(lambda v: True if v in YES else False if v in NO else None)


def pilot_ids():
    corpus = pd.read_csv(CORPUS_CSV, dtype=str)
    euids = set(corpus.assign(r=corpus.sample_rank.astype(int)).nsmallest(20, "r").euid)
    dev, _ = load_records(CORPUS_CSV, 20)
    euids |= set(dev.euid)
    return euids, euids | set(corpus[corpus.euid.isin(euids)].doi)


def ned_sheet(pilot_sources):
    tf = pd.read_sql("SELECT t.object, f.source_id AS source_id, f.evidence_span FROM triples t JOIN findings f USING (finding_id)", db)
    tf["key"] = tf.object.map(norm)
    pilot_keys = set(tf[tf.source_id.isin(pilot_sources)].key)
    pv = RESULTS / "ned_precision_v7.csv"
    if pv.exists():
        pilot_csv = pd.read_csv(pv)
        pilot_keys |= set(pilot_csv.mention.map(norm))
        hit = pilot_csv.context.isin(set(tf[tf.source_id.isin(pilot_sources)].evidence_span)).sum()
        print(f"pilot check: {hit}/{len(pilot_csv)} pilot NED contexts found in the held-out releases (expect all)")
    context = tf.drop_duplicates("key").set_index("key").evidence_span        # first sentence seen, as NED saw it
    ned = pd.read_sql("""SELECT mention_key, mention, method, ontology_id, canonical_name, ontology_kind
                         FROM ned WHERE ontology_id IS NOT NULL""", db)
    pool = ned[~ned.mention_key.isin(pilot_keys)]
    share = pool.method.value_counts(normalize=True)
    s = pd.concat([g.sample(n=min(round(share[m] * N_NED), len(g)), random_state=SEED) for m, g in pool.groupby("method")])
    s = s.sample(frac=1, random_state=SEED)                                    # mix methods so the order gives nothing away
    print(f"NED: {len(ned)} linked, {len(pool)} held out, sampled {len(s)} ({s.method.value_counts().to_dict()})")
    recs, _, _ = driver.execute_query("""MATCH (o:OntologyTerm) WHERE o.id IN $ids
        RETURN o.id AS id, coalesce(o.synonyms, []) AS syn, o.definition AS definition""", ids=list(s.ontology_id), database_=NEO4J_DB)
    term = {r["id"]: r for r in recs}
    return pd.DataFrame({
        "id": s.mention_key.values, "mention": s.mention.values, "sentence": s.mention_key.map(context).values,
        "term_id": s.ontology_id.values, "term_name": s.canonical_name.values, "term_kind": s.ontology_kind.values,
        "term_synonyms": ["; ".join((term.get(i) or {}).get("syn", [])[:15]) for i in s.ontology_id],
        "term_definition": [(term.get(i) or {}).get("definition") or "" for i in s.ontology_id],
        "correct": "", "error_type": ""})


def match_sheet(pilot_euids):
    ex = pd.read_sql("SELECT statement_id, euid, doi, claim_id, resolution_status FROM exaggeration WHERE mode = 'llm'", db)
    held = ex[~ex.euid.isin(pilot_euids)]
    want, picked, used = {"resolved": N_RES, "unresolved": N_UNRES}, {"resolved": [], "unresolved": []}, set()
    for r in held.sample(frac=1, random_state=SEED).to_dict("records"):      # seeded shuffle, then one per release
        b = r["resolution_status"]
        if r["euid"] not in used and len(picked[b]) < want[b]:
            picked[b].append(r)
            used.add(r["euid"])
    print(f"Match: {len(ex)} statements, {len(held)} held out, sampled {len(picked['resolved'])} resolved + "
          f"{len(picked['unresolved'])} unresolved from {len(used)} releases")
    f = pd.read_sql("SELECT finding_id, text, evidence_span FROM findings", db).drop_duplicates("finding_id").set_index("finding_id")
    abstract = dict(db.execute("SELECT doi, abstract FROM records").fetchall())
    r, u = pd.DataFrame(picked["resolved"]), pd.DataFrame(picked["unresolved"])
    matched = pd.DataFrame({
        "id": r.statement_id, "press_restated": r.statement_id.map(f.text), "press_wording": r.statement_id.map(f.evidence_span),
        "paper_restated": r.claim_id.map(f.text), "paper_wording": r.claim_id.map(f.evidence_span),
        "same_finding": "", "press_strength": "", "paper_strength": ""})
    nomatch = pd.DataFrame({
        "id": u.statement_id, "press_restated": u.statement_id.map(f.text), "press_wording": u.statement_id.map(f.evidence_span),
        "abstract": u.doi.map(abstract), "abstract_reports_finding": ""})
    return matched, nomatch


def score():
    ned = pd.read_csv(NED_CSV, dtype=str).merge(pd.read_sql("SELECT mention_key AS id, method FROM ned", db), on="id", how="left")
    ned["ok"] = yn(ned.correct)
    nj = ned[ned.ok.notna()]
    key = pd.read_sql("SELECT statement_id AS id, exaggeration_label, unresolved_reason FROM exaggeration WHERE mode = 'llm'", db)
    ma = pd.read_csv(MATCHED_CSV, dtype=str).merge(key, on="id", how="left").assign(same=lambda d: yn(d.same_finding))
    mt = ma[ma.same.notna()]
    same = mt[mt.same.astype(bool)].dropna(subset=["press_strength", "paper_strength"])
    same = same.assign(judge_label=[label(a, b)[1] for a, b in zip(same.press_strength, same.paper_strength)])
    na = pd.read_csv(NOMATCH_CSV, dtype=str).merge(key, on="id", how="left").assign(rep=lambda d: yn(d.abstract_reports_finding))
    nm = na[na.rep.notna()]
    summary = {
        "ned_precision": wilson(nj.ok.astype(bool).sum(), len(nj)),
        "ned_precision_by_method": {k: wilson(g.ok.astype(bool).sum(), len(g)) for k, g in nj.groupby("method")},
        "ned_error_types": nj[~nj.ok.astype(bool)].error_type.value_counts().to_dict(),
        "match_precision": wilson(mt.same.astype(bool).sum(), len(mt)),
        "label_accuracy_given_match": wilson((same.judge_label == same.exaggeration_label).sum(), len(same)),
        "label_precision_by_pipeline_label": {k: wilson((g.judge_label == k).sum(), len(g)) for k, g in same.groupby("exaggeration_label")},
        "label_confusion_pipeline_x_graded": pd.crosstab(same.exaggeration_label, same.judge_label).to_dict(),
        "no_match_accuracy_R3": wilson((~nm.rep.astype(bool)).sum(), len(nm)),
        "no_match_accuracy_by_reason": {k: wilson((~g.rep.astype(bool)).sum(), len(g)) for k, g in nm.groupby("unresolved_reason")},
        "unfilled_rows": {"ned": int(ned.ok.isna().sum()), "matched": int(len(ma) - len(mt)), "nomatch": int(len(na) - len(nm))},
    }
    SUMMARY.write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--force", action="store_true", help="overwrite existing sheets (erases filled labels)")
    args = ap.parse_args()
    if args.score:
        score()
        sys.exit()
    if any(p.exists() for p in SHEETS) and not args.force:
        sys.exit("sheets exist; use --score, or --force to resample and overwrite")
    euids, sources = pilot_ids()
    print(f"held out: {len(euids)} pilot releases")
    ned_sheet(sources).to_csv(NED_CSV, index=False)
    matched, nomatch = match_sheet(euids)
    matched.to_csv(MATCHED_CSV, index=False)
    nomatch.to_csv(NOMATCH_CSV, index=False)
    print("wrote " + ", ".join(str(p) for p in SHEETS))
