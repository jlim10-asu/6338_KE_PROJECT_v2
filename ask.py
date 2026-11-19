"""Command-line front end for the GraphRAG engine.

  python ask.py "Does the paper support the claim that exercise lowers dementia risk?"
  python ask.py "..." --debug      also print the route and retrieved rows
  python ask.py "..." --baseline   vector-only retrieval (no graph traversal)
"""
import argparse
import json
from s8_qa.graphrag import answer

ap = argparse.ArgumentParser()
ap.add_argument("question")
ap.add_argument("--debug", action="store_true")
ap.add_argument("--baseline", action="store_true")
ap.add_argument("--no-cache", action="store_true")
args = ap.parse_args()

out = answer(args.question, mode="baseline" if args.baseline else "graph", use_cache=not args.no_cache)
print(f"\nROUTE   {out['path_type']} / {out['template_id']}")
if args.debug:
    print(f"REASON  {out['retrieval']['route']['reasoning']}")
    for tid, rows in out["retrieval"]["results"].items():
        print(f"\n--- {tid}: {len(rows)} rows")
        for r in rows[:5]:
            print(json.dumps({k: v for k, v in r.items() if v not in (None, [])}, default=str)[:600])
print(f"\nANSWER\n{out['answer']}")
for r in out["table"] or []:
    print("  " + json.dumps(r, default=str)[:300])
for f in out["flagged"]:
    print(f"FLAGGED sentence {f['sentence'] + 1}: {f['unknown_ids'] or 'no citation'}")
print(f"\nTIMING  {out['timing_ms']}\n")
