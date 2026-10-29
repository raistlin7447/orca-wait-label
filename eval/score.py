#!/usr/bin/env python3
"""Scores labeling policies built from each model's runs: score.py <model> [<model> ...].

The reference is two independent Opus passes (data/ref/pass*_out*.jsonl). A case is clear when both passes
give the same answer with high confidence. A "waiting" answer hides a PR from the review queue, so it is
harmful unless both passes also say "waiting". A wrong "not waiting" only leaves a PR in the review queue,
so it is counted as missed, and only on clear cases.
"""
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load(pattern):
    rows = {}
    for path in glob.glob(os.path.join(HERE, pattern)):
        for line in open(path, encoding="utf-8"):
            if line.strip():
                r = json.loads(line)
                rows[r["id"]] = r
    return rows


def main():
    cases = {c["id"]: c for c in map(json.loads, open(os.path.join(HERE, "cases.jsonl"), encoding="utf-8"))}
    p1, p2 = load("data/ref/pass1_out*.jsonl"), load("data/ref/pass2_out*.jsonl")
    clear = {i: p1[i]["waiting_on_author"] for i in cases
             if i in p1 and i in p2 and p1[i]["waiting_on_author"] == p2[i]["waiting_on_author"]
             and p1[i]["confidence"] == p2[i]["confidence"] == "high"}
    # Both passes say "waiting", at least one with low confidence: a "waiting" answer there is not harmful
    leans_waiting = {i for i in cases if i not in clear and i in p1 and p1[i]["waiting_on_author"] and p2[i]["waiting_on_author"]}
    for kind in ("add", "remove"):
        ids = [i for i in cases if cases[i]["kind"] == kind]
        print(f"{kind}: {sum(i in clear for i in ids)} clear ({sum(clear.get(i, False) for i in ids)} waiting), "
              f"{sum(i in leans_waiting for i in ids)} lean waiting, "
              f"{sum(i not in clear and i not in leans_waiting for i in ids)} lean not waiting or split")

    for model in sys.argv[1:]:
        runs = [load(os.path.relpath(p, HERE)) for p in sorted(glob.glob(os.path.join(HERE, "data", "runs", f"{model}_run*.jsonl")))]
        ids = [i for i in cases if all(i in r for r in runs)]
        consistent = sum(len({r[i]["waiting_on_author"] for r in runs}) == 1 for i in ids)
        print(f"\n{model}: {len(runs)} runs, {len(ids)} cases in all of them, same answer every run on {consistent}")
        # One call per run, versus "waiting" only when every run says so
        policies = [(f"one call (run {n + 1})", lambda i, r=r: r[i]["waiting_on_author"]) for n, r in enumerate(runs)]
        policies.append((f"{len(runs)} of {len(runs)}", lambda i: all(r[i]["waiting_on_author"] for r in runs)))
        for name, says_waiting in policies:
            for kind in ("add", "remove"):
                kind_ids = [i for i in ids if cases[i]["kind"] == kind]
                harmful = [i for i in kind_ids if says_waiting(i) and not clear.get(i, False) and i not in leans_waiting]
                missed = [i for i in kind_ids if clear.get(i) and not says_waiting(i)]
                print(f"  {name:20} {kind:6}  harmful {len(harmful):3} of {len(kind_ids)}   "
                      f"missed {len(missed):3} of {sum(clear.get(i, False) for i in kind_ids)} clear waits"
                      f"   harmful on clear cases: {sum(i in clear for i in harmful)}")


if __name__ == "__main__":
    main()
