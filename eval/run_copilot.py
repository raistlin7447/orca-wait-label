#!/usr/bin/env python3
"""Runs the bot's classifier through the Copilot SDK on the eval cases: run_copilot.py <label> <runs> [--limit N] [--model M].

Same system prompt, schema and cases as run_model.py, one fresh session per case, no tools. Needs
`pip install github-copilot-sdk`. Reads the token from the file named by WAIT_LABEL_COPILOT_TOKEN_FILE and
passes it only to the Copilot client. Writes data/runs/<label>_run<N>.jsonl and skips cases already written,
so an interrupted run resumes. Without --model, Copilot picks the model, as it must on the Free plan.
With --models it only prints the models the token can use, and makes no model call.
"""
import argparse
import asyncio
import json
import os
import sys
import tempfile

from copilot import CopilotClient

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import wait_label as w  # noqa: E402

WORKERS = 4
TIMEOUT = 120
# OpenAI's strict structured output needs a closed object
SCHEMA = {**w.SCHEMA, "additionalProperties": False}


async def judge(client, case, model):
    session = await client.create_session(
        model=model, available_tools=[], skip_custom_instructions=True,
        system_message={"mode": "replace", "content": w.SYSTEM_PROMPT})
    try:
        reply = await session.send_and_wait(case["prompt"], response_schema=SCHEMA, timeout=TIMEOUT)
        verdict = json.loads(reply.data.content)
        usage = await session.rpc.usage.get_metrics()
        return {"id": case["id"], "waiting_on_author": verdict["waiting_on_author"], "reason": verdict["reason"],
                "credits": (usage.total_nano_aiu or 0) / 1e9, "models": sorted(usage.model_metrics)}
    finally:
        await session.disconnect()


async def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("label", nargs="?", default="copilot")
    ap.add_argument("runs", nargs="?", type=int, default=1)
    ap.add_argument("--limit", type=int, help="only the first N cases of each kind")
    ap.add_argument("--model", help="a model the token can use (default: Copilot picks)")
    ap.add_argument("--models", action="store_true", help="list the models the token can use and stop")
    args = ap.parse_args()
    token = open(os.environ["WAIT_LABEL_COPILOT_TOKEN_FILE"], encoding="utf-8").read().strip()

    async with CopilotClient(github_token=token, mode="empty", base_directory=tempfile.mkdtemp()) as client:
        if args.models:
            for m in await client.list_models():
                print(m.id, m.billing)
            return

        cases = [json.loads(l) for l in open(os.path.join(HERE, "cases.jsonl"), encoding="utf-8")]
        if args.limit:
            cases = [c for kind in ("add", "remove") for c in [c for c in cases if c["kind"] == kind][:args.limit]]
        os.makedirs(os.path.join(HERE, "data", "runs"), exist_ok=True)
        gate = asyncio.Semaphore(WORKERS)

        async def guarded(case):
            async with gate:
                try:
                    return await judge(client, case, args.model)
                except Exception as error:
                    print(f"case {case['id']}: {error!r}", file=sys.stderr)
                    return None

        for run in range(1, args.runs + 1):
            path = os.path.join(HERE, "data", "runs", f"{args.label}_run{run}.jsonl")
            done = {json.loads(l)["id"] for l in open(path, encoding="utf-8")} if os.path.exists(path) else set()
            results = await asyncio.gather(*[guarded(c) for c in cases if c["id"] not in done])
            with open(path, "a", encoding="utf-8") as out:
                for r in filter(None, results):
                    out.write(json.dumps(r) + "\n")
            ok = [r for r in results if r]
            print(f"run {run}: {len(ok)} of {len(results)} cases, {sum(r['credits'] for r in ok):.3f} AI credits, "
                  f"models {sorted({m for r in ok for m in r['models']})}")


if __name__ == "__main__":
    asyncio.run(main())
