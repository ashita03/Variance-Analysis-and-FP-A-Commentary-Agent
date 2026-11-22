import copy
import json
from types import SimpleNamespace

from conftest import good_commentary
from narrator import write_commentary


class ScriptedLLM:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def chat(self, messages, tools=None, tool_choice=None):
        self.calls.append(messages)
        return SimpleNamespace(content=self.replies.pop(0), tool_calls=None)


def quiet(*a, **k):
    pass


def test_clean_first_draft_needs_no_revision(sample_run):
    llm = ScriptedLLM([json.dumps(good_commentary(sample_run["facts"]))])
    out = write_commentary(llm, sample_run, on_event=quiet)
    assert out["verification"]["ok"] and out["revisions"] == 0 and len(llm.calls) == 1


def test_bad_number_is_fed_back_and_fixed_on_revision(sample_run):
    good = good_commentary(sample_run["facts"])
    bad = copy.deepcopy(good)
    bad["paragraphs"][1]["text"] = "Summit Jacket's price cut cost $26,315."
    llm = ScriptedLLM([json.dumps(bad), json.dumps(good)])
    out = write_commentary(llm, sample_run, on_event=quiet)
    assert out["verification"]["ok"] and out["revisions"] == 1
    feedback = llm.calls[1][-1]["content"]                      # what the narrator was told
    assert "$26,315" in feedback and "paragraph 2" in feedback


def test_invalid_json_and_code_fences_are_handled(sample_run):
    fenced = "```json\n" + json.dumps(good_commentary(sample_run["facts"])) + "\n```"
    llm = ScriptedLLM(["Sure! Here is the commentary.", fenced])
    out = write_commentary(llm, sample_run, on_event=quiet)
    assert out["verification"]["ok"] and out["revisions"] == 1


def test_gives_up_after_max_revisions_instead_of_looping(sample_run):
    bad = good_commentary(sample_run["facts"])
    bad["headline"] = "Profit fell by $123,456 in October."
    llm = ScriptedLLM([json.dumps(bad)] * 3)
    out = write_commentary(llm, sample_run, max_revisions=2, on_event=quiet)
    assert not out["verification"]["ok"] and out["revisions"] == 2 and len(llm.calls) == 3