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

    def chat(self, messages, tools=None, tool_choice=None, retries=4, on_wait=print):
        """One model call.

        Retries ONLY errors that can fix themselves: rate limits (429), server errors (5xx)
        and network blips. Mistakes like a wrong model name (404) or bad key (401) raise
        immediately, because retrying can never help.
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
                wait = 20 * (attempt + 1)          # free tiers reset per minute
                on_wait(f"  (rate limited, waiting {wait}s, then retrying)")
                time.sleep(wait)
            except APIConnectionError:
                if last:
                    raise
                time.sleep(2 ** attempt * 2)
            except APIStatusError as e:
                if e.status_code < 500 or last:    # 4xx: our fault, fail fast
                    raise
                time.sleep(2 ** attempt * 2)