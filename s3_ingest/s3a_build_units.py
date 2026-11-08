"""FR-1 ingestion: records, extraction units, and numbered sentences (the extract stage extracts from these).

Press releases (one unit per release; long releases split into paragraph windows):
  1. Cut the body at the EurekAlert end mark ("###") and drop contact, funding and metadata paragraphs.
  2. Split into paragraphs and sentences, numbered P{p}.S{s}.
  3. A release of at most PRESS_WINDOW_WORDS words is one unit (chunk_id euid#release). A longer one is split into windows
     of whole paragraphs (euid#w0, #w1, ...); each window also sees the previous paragraph as context it cannot cite.
Abstracts (one unit per DOI, chunk_id doi#abstract):
  1. Candidates: the sources the corpus stage fetched (pmc, pubmed, crossref; data/raw/abstract_sources.csv) plus the
     corpus abstract, in ABSTRACT_SOURCE_PRIORITY.
  2. Quality flags: front_matter, truncated_start, too_short and empty block a candidate; too_long, truncated_end and
     sources_disagree are recorded only. The first candidate in priority without a blocking flag is used.
  3. Sentences are numbered S{n} and section-labeled (BACKGROUND, METHODS, RESULTS, CONCLUSIONS, OTHER, UNLABELED), from the
     source's own section structure when it has one, else from header words in the text. Only RESULTS, CONCLUSIONS and
     UNLABELED sentences can be cited as evidence, so background claims about prior work cannot become findings.
  An abstract whose every candidate has a blocking flag gets no unit when ABSTRACT_SKIP_FLAGGED is True (the record stays).
Tables: records (adds abstract_source, abstract_flags; abstract = the chosen text), chunks (one row per unit; text = the unit's
own sentences), sentences (one row per numbered sentence, with section and citable). Summary: results/ingest_summary.json.
No network: every abstract source was fetched by the corpus stage (1f) and exported with corpus.csv (1g).
"""
import json
from collections import Counter
import pandas as pd
from config import (ABSTRACT_MAX_WORDS as MAX_WORDS, ABSTRACT_MIN_WORDS as MIN_WORDS, ABSTRACT_SKIP_FLAGGED as SKIP_FLAGGED,
                    ABSTRACT_SOURCE_PRIORITY as PRIORITY, ABSTRACT_SOURCES_CSV, CORPUS_CSV, DEV_LIMIT, PRESS_WINDOW_WORDS, RESULTS)
from core.db import db
from s3_ingest.text import (BLOCKING_FLAGS, CITABLE_SECTIONS, abstract_candidates, choose_abstract, clean_press, load_records,
                            number_abstract, number_press, press_windows)


df, before = load_records(CORPUS_CSV, DEV_LIMIT)
print(f"FR-1: kept {len(df)} of {before} records (excluded rows missing required fields); sample {DEV_LIMIT or 'all'}")

fetched = {}
if ABSTRACT_SOURCES_CSV.exists():                     # written by the corpus stage (1g) next to corpus.csv
    src = pd.read_csv(ABSTRACT_SOURCES_CSV, dtype=str).dropna(subset=["text"])
    for doi, source, sections in src[["doi", "source", "sections_json"]].itertuples(index=False):
        fetched.setdefault(doi, {})[source] = json.loads(sections)


chunks, sentences, seen_doi = [], [], set()
stats = Counter()
abstract_source, abstract_flag_text, abstract_text = {}, {}, {}
for r in df.itertuples():
    # ---- abstract: once per DOI, since several releases can announce one paper
    if r.doi not in seen_doi:
        seen_doi.add(r.doi)
        cands = abstract_candidates(fetched.get(r.doi, {}), r.abstract, PRIORITY, MIN_WORDS, MAX_WORDS)
        pick = choose_abstract(cands, PRIORITY)
        abstract_source[r.doi], abstract_flag_text[r.doi], abstract_text[r.doi] = pick["source"], ";".join(pick["flags"]), pick["text"]
        stats[f"abstract_source_{pick['source']}"] += 1
        for f in pick["flags"]:
            stats[f"abstract_flag_{f}"] += 1
        blocked = BLOCKING_FLAGS & set(pick["flags"])
        if blocked and SKIP_FLAGGED:
            stats["abstract_skipped_flagged"] += 1
        else:
            numbered = number_abstract(pick["labeled"])
            citable = {sid for _, sid, cat, _ in numbered if cat in CITABLE_SECTIONS}
            if not citable:                    # every sentence labeled background/methods/other: do not lose the abstract
                citable = {sid for _, sid, _, _ in numbered}
                stats["abstract_no_results_section"] += 1
            if all(cat == "UNLABELED" for _, _, cat, _ in numbered):
                stats["abstract_unlabeled"] += 1
            cid = f"{r.doi}#abstract"
            chunks.append({"chunk_id": cid, "source_type": "abstract", "source_id": r.doi,
                           "text": " ".join(s for _, _, _, s in numbered), "seq": 0})
            sentences += [{"sentence_id": f"{cid}:{sid}", "chunk_id": cid, "source_type": "abstract", "source_id": r.doi,
                           "sid": sid, "para": 1, "idx": n, "section": cat, "citable": int(sid in citable), "text": s}
                          for n, sid, cat, s in numbered]
            stats["abstract_units"] += 1
    # ---- press release
    paras, removed = clean_press(r.press_text)
    stats["press_chars_total"] += len(r.press_text or "")
    stats["press_chars_removed"] += removed
    numbered = number_press(paras)
    if not numbered:
        stats["press_empty_after_cleaning"] += 1
        continue
    windows = press_windows(numbered, PRESS_WINDOW_WORDS)
    stats["press_releases"] += 1
    stats["press_windowed"] += len(windows) > 1
    for w, (owned, context_para) in enumerate(windows):
        cid = f"{r.euid}#release" if len(windows) == 1 else f"{r.euid}#w{w}"
        own = [x for x in numbered if x[0] in owned]
        ctx = [x for x in numbered if x[0] == context_para]
        chunks.append({"chunk_id": cid, "source_type": "press", "source_id": r.euid,
                       "text": "\n\n".join(" ".join(t for q, _, _, t in own if q == p) for p in owned), "seq": w})
        sentences += [{"sentence_id": f"{cid}:{sid}", "chunk_id": cid, "source_type": "press", "source_id": r.euid,
                       "sid": sid, "para": p, "idx": s, "section": "CONTEXT" if (p, s, sid, t) in ctx else "BODY",
                       "citable": int((p, s, sid, t) in own), "text": t}
                      for p, s, sid, t in ctx + own]
        stats["press_units"] += 1
        stats["press_unit_sentences"] += len(own)

df["abstract_source"] = df.doi.map(abstract_source)
df["abstract_flags"] = df.doi.map(abstract_flag_text)
df["abstract"] = df.doi.map(abstract_text).where(df.doi.map(abstract_text).str.len() > 0, df["abstract"])
df.to_sql("records", db, if_exists="replace", index=False)
pd.DataFrame(chunks).to_sql("chunks", db, if_exists="replace", index=False)
pd.DataFrame(sentences).to_sql("sentences", db, if_exists="replace", index=False)
db.commit()

summary = {
    "records": len(df), "records_before_filter": before,
    "press": {"releases": stats["press_releases"], "units": stats["press_units"], "windowed_releases": stats["press_windowed"],
              "empty_after_cleaning": stats["press_empty_after_cleaning"],
              "boilerplate_share_removed": round(stats["press_chars_removed"] / max(stats["press_chars_total"], 1), 3),
              "mean_sentences_per_unit": round(stats["press_unit_sentences"] / max(stats["press_units"], 1), 1),
              "window_words": PRESS_WINDOW_WORDS},
    "abstracts": {"units": stats["abstract_units"], "skipped_flagged": stats["abstract_skipped_flagged"],
                  "source": {k.removeprefix("abstract_source_"): v for k, v in stats.items() if k.startswith("abstract_source_")},
                  "flags": {k.removeprefix("abstract_flag_"): v for k, v in stats.items() if k.startswith("abstract_flag_")},
                  "no_section_headers": stats["abstract_unlabeled"], "no_results_section": stats["abstract_no_results_section"],
                  "priority": PRIORITY, "fetched_sources_available": len(fetched)},
    "sentences": len(sentences),
}
json.dump(summary, open(RESULTS / "ingest_summary.json", "w"), indent=2)
print(json.dumps(summary, indent=2))
