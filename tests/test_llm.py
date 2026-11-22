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