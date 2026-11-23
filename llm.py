"""Thin, provider-agnostic LLM client (any OpenAI-compatible endpoint)."""
import os
import time

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, OpenAI, RateLimitError

load_dotenv()


class LLM:
    def __init__(self, client=None, model=None):
        self.client = client or OpenAI(
            api_key=os.environ["LLM_API_KEY"],
            base_url=os.environ.get("LLM_BASE_URL") or None,
            max_retries=0,   # we handle retries ourselves, so we can be selective
        )
        self.model = model or os.environ["LLM_MODEL"]
        # Optional knobs (unset = provider defaults). Gemini "thinking" models benefit from
        # LLM_REASONING_EFFORT=low in a tool loop: faster and cheaper per step.
        self.reasoning_effort = os.environ.get("LLM_REASONING_EFFORT") or None
        self.temperature = float(os.environ.get("LLM_TEMPERATURE", 0.2))

    def chat(self, messages, tools=None, tool_choice=None, retries=6, on_wait=print):
        """One model call.

        Retries ONLY errors that can fix themselves, waiting longer each time:
          - rate limits (429): free tiers reset per minute
          - server errors (5xx, e.g. 503 'model overloaded') and network blips
        Mistakes like a wrong model name (404) or bad key (401) raise immediately,
        because retrying can never help.
        """
        kwargs = dict(model=self.model, messages=messages, temperature=self.temperature)
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        if tools:
            kwargs["tools"] = tools
        if tool_choice:                      # e.g. "none" = the model may NOT call tools
            kwargs["tool_choice"] = tool_choice
        for attempt in range(retries):
            last = attempt == retries - 1
            try:
                return self.client.chat.completions.create(**kwargs).choices[0].message
            except RateLimitError:
                if last:
                    raise
                self._wait(20 * (attempt + 1), "rate limited", on_wait)
            except (APIConnectionError, APIStatusError) as e:
                status = getattr(e, "status_code", 500)
                if (status < 500 and isinstance(e, APIStatusError)) or last:
                    raise                    # 4xx: our fault, fail fast
                self._wait(min(60, 5 * 2 ** attempt), "provider unavailable", on_wait)

    @staticmethod
    def _wait(seconds, why, on_wait):
        on_wait(f"  ({why}, waiting {seconds}s, then retrying)")
        time.sleep(seconds)