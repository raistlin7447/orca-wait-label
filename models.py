"""The two ways to ask a model the bot's question. Both answer with one JSON object and have no tools."""
import asyncio
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # Claude reports dollars at list price, which is only an estimate on a subscription token
    cost_usd: float = 0.0
    # Copilot reports AI credits
    ai_credits: float = 0.0

    def add(self, other):
        for field in self.__dataclass_fields__:
            setattr(self, field, getattr(self, field) + getattr(other, field))


def model_env():
    """The environment for a model process, which has no use for the GitHub token that changes labels."""
    hidden = {"GH_TOKEN", "GITHUB_TOKEN", "WAIT_LABEL_COPILOT_TOKEN"}
    return {k: v for k, v in os.environ.items() if v and k not in hidden}


class ClaudeModel:
    def __init__(self, model, system_prompt, schema, binary="claude", timeout=300, effort=""):
        self.timeout = timeout
        self.args = [binary, "-p", "--tools", "", "--strict-mcp-config", "--setting-sources", "",
                     "--no-session-persistence", "--model", model, "--system-prompt", system_prompt,
                     "--json-schema", json.dumps(schema), "--output-format", "json"] + (["--effort", effort] if effort else [])

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def judge(self, prompt):
        out = subprocess.run(self.args, input=prompt, capture_output=True, text=True, encoding="utf-8",
                             timeout=self.timeout, env=model_env())
        result = json.loads(out.stdout)
        if result.get("is_error"):
            raise RuntimeError(result.get("result"))
        usage = result.get("usage", {})
        tokens = sum(usage.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        return (result.get("structured_output") or json.loads(result["result"]),
                Usage(1, tokens, usage.get("output_tokens", 0), cost_usd=result.get("total_cost_usd", 0.0)))


class CopilotModel:
    def __init__(self, model, system_prompt, schema, token, timeout=300, effort=""):
        self.model, self.system_prompt, self.token = model, system_prompt, token
        self.timeout, self.effort = timeout, effort or None
        # OpenAI's strict structured output needs a closed object
        self.schema = {**schema, "additionalProperties": False}

    def __enter__(self):
        # Imported here so that a run with the Claude provider does not need the SDK installed
        from copilot import CopilotClient
        self.loop = asyncio.new_event_loop()
        self.client = CopilotClient(github_token=self.token, mode="empty", env=model_env(),
                                    base_directory=tempfile.mkdtemp())
        self.loop.run_until_complete(self.client.start())
        return self

    def __exit__(self, *_):
        self.loop.run_until_complete(self.client.stop())
        self.loop.close()

    async def ask(self, prompt):
        session = await self.client.create_session(
            model=self.model, reasoning_effort=self.effort, available_tools=[], skip_custom_instructions=True,
            system_message={"mode": "replace", "content": self.system_prompt})
        try:
            reply = await session.send_and_wait(prompt, response_schema=self.schema, timeout=self.timeout)
            metrics = await session.rpc.usage.get_metrics()
            return (json.loads(reply.data.content),
                    Usage(1, metrics.last_call_input_tokens, metrics.last_call_output_tokens,
                          ai_credits=(metrics.total_nano_aiu or 0) / 1e9))
        finally:
            await session.disconnect()

    def judge(self, prompt):
        return self.loop.run_until_complete(self.ask(prompt))
