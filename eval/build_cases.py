#!/usr/bin/env python3
"""Builds eval cases from the history of open OrcaSlicer PRs, cut where the bot would have to judge.

An add case ends with a team message that follows the author's activity; a remove case ends with an
author comment or review that follows a team message. Each case's prompt is built exactly as the bot
builds it. Writes data/prs.json (the fetch) and cases.jsonl.
"""
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import wait_label as w  # noqa: E402

PER_KIND = 100


def cases_for(pr):
    """The latest add point and the latest remove point in a PR's history."""
    author = pr["author"]["login"]
    evs = w.events(pr)
    found = {}
    for k in range(len(evs) - 1, -1, -1):
        e, nxt, prev = evs[k], evs[k + 1] if k + 1 < len(evs) else None, evs[k - 1] if k else None
        is_author = e[1] == author
        if "add" not in found and not is_author and e[3] != "review APPROVED" and (nxt is None or nxt[1] == author):
            found["add"] = evs[: k + 1]
        if "remove" not in found and is_author and e[3] != "commit" and prev and prev[1] != author and prev[3] != "review APPROVED":
            found["remove"] = evs[: k + 1]
    return found


def main():
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    raw = os.path.join(HERE, "data", "prs.json")
    if os.path.exists(raw):
        prs = json.load(open(raw, encoding="utf-8"))
    else:
        prs = w.search(f"repo:{w.REPO_OWNER}/{w.REPO_NAME} is:pr is:open")
        json.dump(prs, open(raw, "w", encoding="utf-8"))

    pools = {"add": [], "remove": []}
    for pr in prs:
        if not pr["author"] or w.is_bot(pr["author"]["login"]):
            continue
        for kind, evs in cases_for(pr).items():
            pools[kind].append({"kind": kind, "number": pr["number"], "prompt": w.prompt_for(pr, evs)})

    rng = random.Random(29)
    cases = [c for kind in ("add", "remove") for c in rng.sample(pools[kind], min(PER_KIND, len(pools[kind])))]
    for i, c in enumerate(cases):
        c["id"] = i
    with open(os.path.join(HERE, "cases.jsonl"), "w", encoding="utf-8") as out:
        for c in cases:
            out.write(json.dumps(c) + "\n")
    print({kind: len(pool) for kind, pool in pools.items()}, "->", len(cases), "cases")


if __name__ == "__main__":
    main()
