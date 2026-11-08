# Reconciling Medical Press Release Statements Against the Academic Record

A knowledge graph and LLM pipeline. It extracts exposure → outcome findings from EurekAlert! medical press releases and
their source papers' abstracts, links entities to reference vocabularies, labels each press statement **overstated /
aligned / understated** against its own paper, and loads the result into Neo4j.

Course project: CS 6399 / CS 6338, Fall 2026.

## Setup
- Python 3.10+, Neo4j 5.26 with Graph Data Science (needed only for `communities`), an Anthropic API key.

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...  NEO4J_PASSWORD=...  OPENALEX_API_KEY=...       # OpenAlex key: free, openalex.org/settings/api
export CONTACT_EMAIL=you@example.edu        # identifies you to EurekAlert (crawler), Crossref and Europe PMC
export NCBI_API_KEY=...  S2_API_KEY=...       # optional: faster PubMed/PMC and Semantic Scholar limits
```
`config.py` holds every setting and is committed. Secrets come only from environment variables; never write them into it.

## Layout
Folders and stage scripts are numbered in run order (`s1b` runs after `s1a`). Python names can't start with a digit,
hence the `s` prefix. Unnumbered files are helpers, or prompts and schemas used by that folder's stages.

```
run.py                 the runner: stage order lives here
config.py              settings (no secrets)
core/                  shared: db.py (SQLite + Neo4j), llm.py (cached Claude calls), embed.py, text.py, types.py
s1_corpus/             s1a_pool, s1b_topics, s1c_sample, s1d_crawl, s1e_enrich, s1f_abstracts, s1g_corpus; eureka.py (helpers)
s2_setup/              s2a_schema.py (Neo4j schema), s2b_doid.py, s2c_hetionet.py, cypher/schema.cypher
s3_ingest/             s3a_build_units.py (no network), text.py (cleaning, numbering, abstract parsing and choice rules)
s4_extract/            s4a_extract.py (findings), s4b_ned.py (entity linking), prompts.py, schemas.py
s5_reconcile/          s5a_score.py (statement -> claim, labels), matching.py, prompts.py, schemas.py
s6_graph/              s6a_load.py, s6b_verify.py, reset.py, cypher/verify_load.cypher
s7_analysis/           s7a_communities.py (Leiden), s7b_summaries.py (community reports), prompts.py, schemas.py
tests/                 unit tests (no database or network)
data/                  (not tracked) raw inputs and pipeline.db
results/               run summaries written by the stages
```

## Running
```bash
python run.py list                    # stages and groups
python run.py corpus                  # 1. build data/raw/corpus.csv from eurekalert_2025.sqlite (see below; set SCOPE first)
python run.py setup                   # 2. schema + DOID + Hetionet, once per Neo4j database
python run.py pipeline                # 3. units -> extract -> ned -> score -> load -> verify (only Claude goes online)
python run.py analysis                # 4. communities -> summaries -> verify
python run.py extract ned             # any stages, in the order given; numbers work too: python run.py 4a 4b
python run.py summaries -- --dry-run  # arguments after "--" go to a single stage
python run.py reset                   # clear derived data before a re-run (keeps ontology, LLM cache, abstracts)
```
Smoke-test first: set `DEV_LIMIT = 20` in `config.py`, run `units extract ned`, and check `results/ned_summary.json`.
Responses are cached by prompt, so raising `DEV_LIMIT` afterwards reuses them.

## Building the study sample (`s1_corpus/`)
The stages read the SQLite file your ETL notebooks built (`data/raw/eurekalert_2025.sqlite`):
`releases` (Eurekalert_local_etl_1: 566,566 JSON records → 152,754 from 2018 on with a DOI), `papers`
(academic_papers_enriched_2: OpenAlex metadata and abstracts) and `fulltext` (Eurekalert_press_papers_full_3: crawled
body text). The EurekAlert! dataset (Zhang et al., 2025, doi:10.34894/EZO4JE) has no release text, so body text is
crawled. The stages add their own `s1_*` tables and new rows; they never drop the notebooks' data.

**A narrowed scope is required:** set `SCOPE["subfields"]` in `config.py` (OpenAlex subfield IDs or names).
`SCOPE["fields"]` can name more than one OpenAlex field; the default is brain and mind across Medicine and Neuroscience.

```bash
python run.py 1a                 # pool: SCOPE fields + years, every stage-1 field present (title, date, abstract, journal, authors)
python run.py 1b                 # OpenAlex subfield/topic per pool DOI (~350 calls for 35,000 DOIs)
python run.py 1c -- --list       # releases per subfield in your pool -> choose SCOPE["subfields"]
python run.py 1c -- --list-topics   # optional: topics inside the chosen subfields -> SCOPE["topics"]
python run.py 1c                 # rank the scoped pool in seeded order (SAMPLE_SEED)
python run.py 1d                 # crawl body text in rank order until SAMPLE_SIZE have it (reuses rows already crawled)
python run.py 1e                 # Crossref (retraction + abstract) and Europe PMC pmcid, for those candidates only
python run.py 1f                 # PMC + PubMed abstracts; judge each with the units stage's rules; Semantic Scholar backfill
python run.py fill               # 1d -> 1e -> 1f again, until 1f reports no free places
python run.py 1g                 # the sample -> data/raw/corpus.csv + data/raw/abstract_sources.csv
```
- `results/sample_report.json` has the row count after every step (pool → field → each required field → years →
  subfield → ranked → crawled → sample), for the report's dataset table.
- Every scoped release gets a rank, and the crawl stops at the target, so no oversample size is needed. A release whose
  page has no body text, or whose paper has no usable abstract after the backfill, is skipped, and the next rank takes
  its place (`python run.py fill`).
- **Abstracts are fetched once, here, and only for the sample.** 1a does not require one. 1e stores Crossref's, 1f adds
  PMC and PubMed, judges them with the same rules the units stage uses to choose (blocking flags: front matter, truncated
  start, too short, empty), and runs the Semantic Scholar backfill only when nothing usable was found. The units stage then
  reads `abstract_sources.csv` and needs no network.
- The crawler is the notebook's parser, unchanged: robots.txt check, 1 request per second, a user agent that names the
  project and `CONTACT_EMAIL`, and pages that returned 404 / 410 are never retried.

**corpus.csv columns:** sample_rank, scope_rank, euid, doi, domain, oa_subfield_id, oa_subfield, oa_topic_id, oa_topic,
body_text, paper_title, abstract, abstract_source, publication_date, year, authors_json, journal, issn_l,
cited_by_count, primary_field, oa_is_retracted, cr_retracted, pmcid, fulltext_status, releases_per_paper, plus release
fields (release_title, post_time, release_institution, release_article_title, method_of_research, subject_of_research).

## Other inputs (`data/raw/`)
| File | Source | Used by |
|---|---|---|
| `doid.owl` | https://purl.obolibrary.org/obo/doid.owl | `doid` |
| `hetionet-v1.0-nodes.tsv` | https://github.com/hetio/hetionet (`hetnet/tsv`; nodes only) | `hetionet` |

## Stages
| # | Stage | Script | Output |
|---|---|---|---|
| 1a | `corpus-pool` | `s1_corpus/s1a_pool.py` | `s1_pool` table |
| 1b | `corpus-topics` | `s1_corpus/s1b_topics.py` | `s1_topics` table |
| 1c | `corpus-sample` | `s1_corpus/s1c_sample.py` | `s1_ranked` table; `results/scope_subfields.csv` with `--list` |
| 1d | `corpus-crawl` | `s1_corpus/s1d_crawl.py` | `fulltext` rows |
| 1e | `corpus-enrich` | `s1_corpus/s1e_enrich.py` | `papers` columns (cr_retracted, pmcid); Crossref rows in `abstract_sources` |
| 1f | `corpus-abstracts` | `s1_corpus/s1f_abstracts.py` | PMC / PubMed rows in `abstract_sources`; `s1_abstracts` verdicts; backfilled `papers.abstract` |
| 1g | `corpus-export` | `s1_corpus/s1g_corpus.py` | `data/raw/corpus.csv`, `data/raw/abstract_sources.csv`, `results/sample_report.json` |
| 2a | `schema` | `s2_setup/s2a_schema.py` | Neo4j constraints and indexes |
| 2b, 2c | `doid`, `hetionet` | `s2_setup/s2b_doid.py`, `s2c_hetionet.py` | `OntologyTerm` nodes |
| 3a | `units` | `s3_ingest/s3a_build_units.py` | `records`, `chunks`, `sentences`; `results/ingest_summary.json` |
| 4a | `extract` | `s4_extract/s4a_extract.py` | `findings`, `triples`, `rejections`: minimal head-concept entities with types |
| 4b | `ned` | `s4_extract/s4b_ned.py` | `ned`; `results/ned_summary.json`: type-routed linking (`NED_KINDS_BY_TYPE`) |
| 5a | `score` | `s5_reconcile/s5a_score.py` | `exaggeration*` tables; `results/exaggeration_summary.json` |
| 6a | `load` | `s6_graph/s6a_load.py` | Neo4j nodes and edges |
| 6b | `verify` | `s6_graph/s6b_verify.py` | `results/load_verification.json` (also run after 7a–7b) |
| 7a | `communities` | `s7_analysis/s7a_communities.py` | `Community` nodes; `results/community_summary.json` |
| 7b | `summaries` | `s7_analysis/s7b_summaries.py` | community reports; `results/community_review.csv` |
| – | `reset` | `s6_graph/reset.py` | clears derived data before a re-run |

## Reproducibility
- Every LLM response is cached in `data/pipeline.db`, keyed by model, the stage's prompt version, effort, tag and prompt
  text. Each `prompts.py` holds its own version (`EXTRACT_VERSION`, `NED_VERSION`, `ADJUDICATE_VERSION`,
  `COMMUNITY_VERSION`): bump it after editing that file, and only that stage's cached responses are invalidated.
- Fixed seeds: study sample (`SAMPLE_SEED`), dev sample (42), Leiden (`COMM_SEED`).
- Neo4j writes use `UNWIND` + `MERGE` on constrained keys, so every stage can be re-run.
- `exaggeration` holds one row per press statement and mode (`embed` baseline, `llm`); the load stage reads `EX_MODE`'s rows.

## Tests
```bash
python tests/test_corpus.py && python tests/test_ingest.py && python tests/test_schemas.py && python tests/test_exaggeration.py
```
