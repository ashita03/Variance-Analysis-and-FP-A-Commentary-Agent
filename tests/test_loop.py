"""Test the agent LOOP with a scripted fake model: no API key, free, deterministic."""
import itertools
import json
from types import SimpleNamespace

from investigator import investigate

_ids = itertools.count(1)


def call(name, **args):
    c = SimpleNamespace(id=f"c{next(_ids)}", function=SimpleNamespace(
        name=name, arguments=json.dumps(args)))
    return SimpleNamespace(content=None, tool_calls=[c])


def say(text):
    return SimpleNamespace(content=text, tool_calls=None)


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.tool_choices = []

    def chat(self, messages, tools=None, tool_choice=None):
        self.tool_choices.append(tool_choice)
        return self.replies.pop(0)


def quiet(*a, **k):
    pass


def test_full_loop_recovers_from_a_bad_query_and_records_evidence_backed_finding():
    llm = ScriptedLLM([
        call("get_material_variances", month="2026-10-01"),
        call("run_query", sql="DROP TABLE revenue"),                       # rejected
        call("run_query", sql="SELECT region, SUM(revenue_actual - revenue_budget) v "
                              "FROM revenue WHERE month = '2026-10-01' GROUP BY region"),
        call("record_finding", summary="West is $37.6K under budget on volume.",
             driver_type="volume", amount_usd=-37585, evidence_query_ids=["q2"]),
        say("All material variances explained."),
    ])
    r = investigate(llm, on_event=quiet)
    assert r["steps"] == 5 and not r["hit_step_cap"] and not r["wrapped_up"]
    assert len(r["findings"]) == 1 and r["findings"][0]["evidence_query_ids"] == ["q2"]
    tool_msgs = [m["content"] for m in r["transcript"] if m["role"] == "tool"]
    assert any('"error"' in m for m in tool_msgs)          # the DROP was fed back as an error


def test_step_cap_stops_a_runaway_agent_then_forces_a_summary_with_tools_disabled():
    llm = ScriptedLLM([call("get_material_variances", month="2026-10-01") for _ in range(3)]
                      + [say("Summary after the cap.")])
    r = investigate(llm, max_steps=3, on_event=quiet)
    assert r["steps"] == 3 and r["hit_step_cap"] and r["wrapped_up"]
    assert r["summary"] == "Summary after the cap."
    assert llm.tool_choices[-1] == "none" and llm.tool_choices[:-1] == [None] * 3


def test_tool_calls_during_wrapup_are_ignored_not_executed():
    llm = ScriptedLLM([call("get_material_variances", month="2026-10-01") for _ in range(2)]
                      + [call("run_query", sql="SELECT 1")])      # a stubborn model
    r = investigate(llm, max_steps=2, on_event=quiet)
    assert r["wrapped_up"] and r["summary"] == ""
    assert len(r["queries"]) == 2                                 # the extra call never ran


def test_bad_json_arguments_do_not_crash_the_loop():
    bad = SimpleNamespace(id="cX", function=SimpleNamespace(name="run_query", arguments="{oops"))
    llm = ScriptedLLM([SimpleNamespace(content=None, tool_calls=[bad]), say("done")])
    r = investigate(llm, on_event=quiet)
    assert not r["hit_step_cap"]


def test_agent_is_nudged_when_two_steps_remain():
    llm = ScriptedLLM([call("get_material_variances", month="2026-10-01") for _ in range(4)]
                      + [say("wrapped up")])
    r = investigate(llm, max_steps=4, on_event=quiet)
    nudges = [m for m in r["transcript"] if m["role"] == "user" and "steps left" in m["content"]]
    assert len(nudges) == 1


def test_provider_extras_on_tool_calls_survive_the_round_trip():
    from openai.types.chat import ChatCompletionMessage
    from investigator import _assistant_dict
    msg = ChatCompletionMessage.model_validate({
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "run_query", "arguments": "{}"},
                        "extra_content": {"google": {"thought_signature": "sig123"}}}]})
    d = _assistant_dict(msg)["tool_calls"][0]
    assert d["extra_content"]["google"]["thought_signature"] == "sig123"
    assert d["function"]["name"] == "run_query" and d["type"] == "function"