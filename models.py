"""The model providers, which answer the bot's question with one JSON object and have no tools."""
import asyncio
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # Claude reports dollars at list price, which is only an estimate on a subscription token
    cost_usd: float = 0.0
    ai_credits: float = 0.0

    def add(self, other):
        for field in self.__dataclass_fields__:
            setattr(self, field, getattr(self, field) + getattr(other, field))


class BadSetting(Exception):
    """An input the provider can't use, such as a model it doesn't have."""


class UnusableAnswer(Exception):
    """The model answered, but not with a verdict the bot can read."""


CLAUDE_TOKENS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
LOOKUP_TRIES, LOOKUP_RETRY_DELAY, LOOKUP_TIMEOUT = 3, 10, 30
SECRET_WORDS = ("TOKEN", "SECRET", "PASSWORD", "KEY")


def one_line(text):
    """Text for the log, where a line starting with :: would be a workflow command."""
    return " ".join(str(text).split())


def answer_json(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        # The text is the model's view of a named contributor's PR, so it stays out of the log
        raise UnusableAnswer("the answer isn't JSON") from None


def check_effort(model, effort, levels):
    """Stops the run for an effort the model doesn't take, naming the levels it does."""
    if effort and not levels:
        raise BadSetting(f"effort must be empty for {model}, which takes no effort setting, not {effort!r}")
    if effort and effort not in levels:
        raise BadSetting(f"effort must be one of {', '.join(levels)} for {model}, not {effort!r}")


def model_env(keep=()):
    """The environment for a model process, with every likely secret removed except those named in keep."""
    return {k: v for k, v in os.environ.items()
            if v and (k in keep or not any(word in k.upper() for word in SECRET_WORDS))}


class ClaudeModel:
    def __init__(self, model, system_prompt, schema, binary="claude", timeout=300, effort=""):
        self.model, self.effort, self.timeout = model, effort, timeout
        self.binary, self.system_prompt, self.schema = binary, system_prompt, schema

    @property
    def args(self):
        return [self.binary, "-p", "--tools", "", "--strict-mcp-config", "--setting-sources", "",
                "--no-session-persistence", "--model", self.model, "--system-prompt", self.system_prompt,
                "--json-schema", json.dumps(self.schema), "--output-format", "json"] + (["--effort", self.effort] if self.effort else [])

    def __enter__(self):
        info = self.lookup(f"/v1/models/{quote(self.model, safe='')}")
        if info is None:
            listing = self.lookup("/v1/models?limit=1000")
            # A gateway set with ANTHROPIC_BASE_URL may not list models at all
            if listing is None:
                print("The model list isn't available, so model and effort go to Claude unchecked")
                return self
            available = listing["data"]
            # Some gateways list models without looking them up by ID
            exact = [m for m in available if m["id"] == self.model]
            # A family name such as sonnet means its newest model, as in claude-sonnet-5-5
            family = exact or [m for m in available if m["id"].split("-")[1:2] == [self.model.lower()]]
            if not family:
                raise BadSetting(f"model must be one of {', '.join(m['id'] for m in available)}, or a family such as sonnet, not {self.model!r}")
            info = max(family, key=lambda m: m.get("created_at", ""))
            if not exact:
                print(f"Using {info['id']} for model {self.model}")
            # The CLI gets the full ID, so that it runs the model checked here
            self.model = info["id"]
        effort = (info.get("capabilities") or {}).get("effort")
        levels = [k for k, v in (effort or {}).items() if isinstance(v, dict) and v.get("supported")]
        # A gateway's model list may leave out what each model supports
        if effort is None or (effort.get("supported") and not levels):
            if self.effort:
                print(f"The model list doesn't say which efforts {self.model} takes, so effort goes to Claude unchecked")
            return self
        check_effort(self.model, self.effort, levels)
        return self

    def __exit__(self, *_):
        pass

    def lookup(self, path):
        """An answer from Anthropic's Models API, or None when it has no such model."""
        headers = {"anthropic-version": "2023-06-01"}
        if os.environ.get("ANTHROPIC_API_KEY"):
            headers["x-api-key"] = os.environ["ANTHROPIC_API_KEY"]
        else:
            headers.update({"Authorization": f"Bearer {os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', '')}",
                            "anthropic-beta": "oauth-2025-04-20"})
        base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        url = base.rstrip("/") + path
        try:
            request = urllib.request.Request(url, headers=headers)
        except ValueError:
            raise BadSetting(f"ANTHROPIC_BASE_URL must be a full URL, such as https://api.anthropic.com, not {base!r}") from None
        for attempt in range(1, LOOKUP_TRIES + 1):
            try:
                with urllib.request.urlopen(request, timeout=LOOKUP_TIMEOUT) as response:
                    body = response.read()
                try:
                    return json.loads(body)
                except ValueError:
                    raise RuntimeError(f"Anthropic's model list at {url} didn't answer with JSON") from None
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    return None
                problem = f"answered {error.code}: {error.read().decode(errors='replace')[:300]}"
                # Only a rate limit or a server error can clear up on a retry
                if error.code != 429 and error.code < 500:
                    break
            except OSError as error:
                problem = f"couldn't be reached: {error}"
            if attempt < LOOKUP_TRIES:
                time.sleep(LOOKUP_RETRY_DELAY)
        raise RuntimeError(f"Anthropic's model list at {url} {problem}")

    def judge(self, prompt):
        try:
            out = subprocess.run(self.args, input=prompt, capture_output=True, text=True, encoding="utf-8",
                                 timeout=self.timeout, env=model_env(CLAUDE_TOKENS))
        except subprocess.TimeoutExpired:
            # The default message quotes the whole command, with the system prompt, in the log
            raise RuntimeError(f"claude gave no answer in {self.timeout} seconds") from None
        try:
            result = json.loads(out.stdout)
        except ValueError:
            raise RuntimeError((out.stderr or out.stdout or f"claude exited with {out.returncode}").strip()[:500])
        if result.get("is_error"):
            raise RuntimeError(result.get("result"))
        usage = result.get("usage", {})
        tokens = sum(usage.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        return (result.get("structured_output") or answer_json(result["result"]),
                Usage(1, tokens, usage.get("output_tokens", 0), cost_usd=result.get("total_cost_usd", 0.0)))


class CopilotModel:
    def __init__(self, model, system_prompt, schema, token, timeout=300, effort=""):
        self.model, self.system_prompt, self.token = model, system_prompt, token
        self.timeout, self.effort = timeout, effort or None
        # The Copilot SDK's structured output needs a closed object
        self.schema = {**schema, "additionalProperties": False}

    def __enter__(self):
        # Imported here so that a run with the Claude provider does not need the SDK installed
        from copilot import CopilotClient
        self.loop = asyncio.new_event_loop()
        self.client = CopilotClient(github_token=self.token, mode="empty", env=model_env(),
                                    base_directory=tempfile.mkdtemp())
        try:
            self.loop.run_until_complete(self.client.start())
            if self.model:
                self.check(self.loop.run_until_complete(self.client.list_models()))
        except BadSetting:
            self.__exit__()
            raise
        except Exception as error:
            self.__exit__()
            raise RuntimeError(f"Copilot couldn't start: {error}") from None
        return self

    def check(self, available):
        info = next((m for m in available if m.id == self.model), None)
        # Copilot answers with another model when it doesn't know the one asked for
        if info is None:
            raise BadSetting(f"model must be one of {', '.join(m.id for m in available)}, not {self.model!r}")
        check_effort(self.model, self.effort, info.supported_reasoning_efforts or [])

    def __exit__(self, *_):
        # A client that fails to stop gets only a warning, so it can't fail a finished run or hide why the check failed
        try:
            self.loop.run_until_complete(self.client.stop())
        except Exception as error:
            print(f"::warning::The Copilot client didn't stop cleanly: {one_line(error)}")
        finally:
            self.loop.close()

    async def ask(self, prompt):
        session = await self.client.create_session(
            model=self.model, reasoning_effort=self.effort, available_tools=[], skip_custom_instructions=True,
            system_message={"mode": "replace", "content": self.system_prompt})
        try:
            reply = await session.send_and_wait(prompt, response_schema=self.schema, timeout=self.timeout)
            metrics = await session.rpc.usage.get_metrics()
            return (answer_json(reply.data.content),
                    Usage(1, metrics.last_call_input_tokens, metrics.last_call_output_tokens,
                          ai_credits=(metrics.total_nano_aiu or 0) / 1e9))
        finally:
            await session.disconnect()

    def judge(self, prompt):
        return self.loop.run_until_complete(self.ask(prompt))
