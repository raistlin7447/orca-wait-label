#!/usr/bin/env python3
"""Runs the bot's real classifier call on every eval case: run_model.py <model> <runs>.

Each case is one fresh `claude -p` call, as in production, with the same WAIT_LABEL_* settings. Reads the
OAuth token from the file named by WAIT_LABEL_TOKEN_FILE and passes it only to the claude process. Writes data/runs/<model>_run<N>.jsonl
and skips cases already written, so an interrupted run resumes.
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import wait_label as w  # noqa: E402

WORKERS = 4


def main():
    model, runs = sys.argv[1], int(sys.argv[2])
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = open(os.environ["WAIT_LABEL_TOKEN_FILE"], encoding="utf-8").read().strip()
    cases = [json.loads(l) for l in open(os.path.join(HERE, "cases.jsonl"), encoding="utf-8")]
    os.makedirs(os.path.join(HERE, "data", "runs"), exist_ok=True)

    for run in range(1, runs + 1):
        path = os.path.join(HERE, "data", "runs", f"{model}_run{run}.jsonl")
        done = {json.loads(l)["id"] for l in open(path, encoding="utf-8")} if os.path.exists(path) else set()
        todo = [c for c in cases if c["id"] not in done]

        def judge(case):
            try:
                v = w.classify(case["prompt"], model)
                return {"id": case["id"], "waiting_on_author": v["waiting_on_author"], "reason": v["reason"]}
            except Exception as error:
                print(f"case {case['id']}: {error}", file=sys.stderr)
                return None

        with open(path, "a", encoding="utf-8") as out, ThreadPoolExecutor(WORKERS) as pool:
            for result in pool.map(judge, todo):
                if result:
                    out.write(json.dumps(result) + "\n")
                    out.flush()
        print(f"{model} run {run}: {len(todo)} judged")


if __name__ == "__main__":
    main()
