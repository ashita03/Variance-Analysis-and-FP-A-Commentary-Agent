from llm import LLM


class FakeClient:
    """Mimics client.chat.completions.create(...) and records what it was sent."""
    def __init__(self):
        self.seen = {}
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.seen = kwargs
        msg = type("M", (), {"content": "ok", "tool_calls": None})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()


def test_optional_env_settings_are_passed_through(monkeypatch):
    monkeypatch.setenv("LLM_REASONING_EFFORT", "low")
    monkeypatch.setenv("LLM_TEMPERATURE", "1.0")
    c = FakeClient()
    LLM(client=c, model="m").chat([{"role": "user", "content": "hi"}])
    assert c.seen["reasoning_effort"] == "low" and c.seen["temperature"] == 1.0


def test_defaults_send_no_reasoning_effort(monkeypatch):
    monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("LLM_TEMPERATURE", raising=False)
    c = FakeClient()
    LLM(client=c, model="m").chat([{"role": "user", "content": "hi"}], tool_choice="none")
    assert "reasoning_effort" not in c.seen and c.seen["temperature"] == 0.2
    assert c.seen["tool_choice"] == "none"


def _err(cls, code):
    try:
        import httpx2 as httpx
    except ImportError:
        import httpx
    r = httpx.Response(code, request=httpx.Request("POST", "http://x"))
    return cls("boom", response=r, body=None)


class FlakyClient(FakeClient):
    def __init__(self, errors):
        super().__init__()
        self.errors, self.calls = list(errors), 0

    def create(self, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return super().create(**kwargs)


def test_overloaded_provider_503_is_retried_with_growing_waits(monkeypatch):
    import openai
    waits = []
    monkeypatch.setattr("llm.time.sleep", waits.append)
    c = FlakyClient([_err(openai.InternalServerError, 503)] * 3)
    out = LLM(client=c, model="m").chat([{"role": "user", "content": "hi"}], on_wait=lambda m: None)
    assert out.content == "ok" and c.calls == 4
    assert waits == [5, 10, 20]


def test_wrong_model_name_404_fails_immediately(monkeypatch):
    import openai
    import pytest
    monkeypatch.setattr("llm.time.sleep", lambda s: None)
    c = FlakyClient([_err(openai.NotFoundError, 404)] * 5)
    with pytest.raises(openai.NotFoundError):
        LLM(client=c, model="m").chat([{"role": "user", "content": "hi"}])
    assert c.calls == 1