"""Thin, provider-agnostic LLM client (any OpenAI-compatible endpoint)."""
import os
import time

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


class LLM:
    def __init__(self, client=None, model=None):
        self.client = client or OpenAI(
            api_key=os.environ["LLM_API_KEY"],
            base_url=os.environ.get("LLM_BASE_URL") or None,
        )
        self.model = model or os.environ["LLM_MODEL"]

    def chat(self, messages, tools=None, retries=3):
        """One model call. Retries with backoff (free tiers rate-limit often)."""
        kwargs = dict(model=self.model, messages=messages, temperature=0.2)
        if tools:
            kwargs["tools"] = tools
        for attempt in range(retries):
            try:
                return self.client.chat.completions.create(**kwargs).choices[0].message
            except Exception:
                if attempt == retries - 1:
                    raise
                time.sleep(2 ** attempt * 2)