"""1c: restrict the pool to SCOPE's subfields (and topics) and rank it in a seeded random order.

Every scoped release gets a sample_rank; 1d crawls in that order and 1f keeps the first SAMPLE_SIZE with body text, so no
oversample size has to be guessed. A narrowed scope is required: SCOPE['subfields'] must name at least one subfield.

Usage:
  python run.py 1c -- --list                 releases per subfield in the pool (choose SCOPE['subfields'])
  python run.py 1c -- --list-topics          releases per topic inside the chosen subfields (optional SCOPE['topics'])
  python run.py 1c                           rank the scoped pool -> table s1_ranked
"""
import argparse
import pandas as pd
from config import SAMPLE_SEED, SCOPE
from s1_corpus.eureka import connect, get_meta, print_funnel, scope_key, set_meta, subfield_filter


def scoped_pool(conn):
    return pd.read_sql("""SELECT p.euid, p.doi, t.found, t.topic_id, t.topic, t.subfield_id, t.subfield, t.field
                          FROM s1_pool p LEFT JOIN s1_topics t ON t.doi = p.doi ORDER BY p.euid""", conn)


def rank(df, scope, seed):
    """Returns (ranked DataFrame, funnel steps). df: scoped_pool() rows."""
    if not scope.get("subfields"):
        raise SystemExit("SCOPE['subfields'] is empty: a narrowed scope is required. "
                         "Run `python run.py 1c -- --list` and set SCOPE['subfields'] in config.py")
    funnel = []
    known = df[df.found == 1]
    funnel.append(["OpenAlex topic known", len(known)])
    names = dict(known[["subfield_id", "subfield"]].dropna().drop_duplicates().itertuples(index=False))
    ids = subfield_filter(scope["subfields"], {int(k): v for k, v in names.items()})
    df = known[known.subfield_id.isin(ids)]
    funnel.append([f"subfields {sorted(ids)} ({'; '.join(names.get(i, '?') for i in sorted(ids))})", len(df)])
    if scope.get("topics"):
        df = df[df.topic_id.isin(scope["topics"])]
        funnel.append([f"topics {scope['topics']}", len(df)])
    ranked = df.sample(frac=1, random_state=seed).reset_index(drop=True)
    ranked.insert(0, "sample_rank", ranked.index + 1)
    funnel.append([f"ranked in seeded order (seed {seed})", len(ranked)])
    return ranked[["sample_rank", "euid", "doi", "field", "subfield_id", "subfield", "topic_id", "topic"]], funnel


def counts(df, by, where=None):
    d = df[df.found == 1]
    if where is not None:
        d = d[where(d)]
    return d.groupby(by).size().rename("releases").reset_index().sort_values("releases", ascending=False)


def main():
    from config import RESULTS
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="releases per subfield in the pool")
    ap.add_argument("--list-topics", action="store_true", help="releases per topic inside SCOPE['subfields']")
    args = ap.parse_args()
    conn = connect()
    df = scoped_pool(conn)
    if args.list:
        c = counts(df, ["field", "subfield_id", "subfield"])
        c.to_csv(RESULTS / "scope_subfields.csv", index=False)
        print(c.to_string(index=False))
        print(f"\npool releases without an OpenAlex topic yet: {int((df.found != 1).sum()):,} (run 1b)")
        return
    if args.list_topics:
        names = dict(df[["subfield_id", "subfield"]].dropna().drop_duplicates().itertuples(index=False))
        ids = subfield_filter(SCOPE.get("subfields") or [], {int(k): v for k, v in names.items()})
        c = counts(df, ["subfield", "topic_id", "topic"], where=lambda d: d.subfield_id.isin(ids))
        c.to_csv(RESULTS / "scope_topics.csv", index=False)
        print(c.to_string(index=False))
        return
    ranked, funnel = rank(df, SCOPE, SAMPLE_SEED)
    previous = get_meta(conn, "scope")
    if previous and previous != scope_key(SCOPE):
        print(f"scope changed since the last ranking:\n  was {previous}\n  now {scope_key(SCOPE)}")
    ranked.to_sql("s1_ranked", conn, if_exists="replace", index=False)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_s1_ranked_euid ON s1_ranked(euid)")
    set_meta(conn, "scope", scope_key(SCOPE))
    set_meta(conn, "funnel_sample", funnel)
    print_funnel(funnel)


if __name__ == "__main__":
    main()
