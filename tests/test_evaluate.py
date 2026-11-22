import copy
import json

from conftest import good_commentary
from evaluate import run_live, score_run, stress_test_verifier
from test_loop import ScriptedLLM, call, say
from verifier import verify_commentary


def comm_out(run):
    c = good_commentary(run["facts"])
    return {"commentary": c, "verification": verify_commentary(c, run), "revisions": 0}


def test_a_correct_run_passes_every_check(sample_run):
    s = score_run(sample_run, comm_out(sample_run))
    assert s["pass"], s
    assert s["passed"] == s["total"] == 7 and s["extra_findings"] == 0


def test_missing_driver_fails_that_check(sample_run):
    run = copy.deepcopy(sample_run)
    run["findings"] = [f for f in run["findings"] if f["id"] != "f1"]
    s = score_run(run, None)
    assert not s["checks"]["west_volume_shortfall"] and not s["pass"]


def test_wrong_amount_and_wrong_type_are_caught(sample_run):
    run = copy.deepcopy(sample_run)
    run["findings"][1]["amount_usd"] = -20000.0          # price cut, off by >1%
    run["findings"][0]["driver_type"] = "price"          # volume shortfall mislabelled
    s = {d["id"]: d for d in score_run(run, None)["drivers"]}
    assert not s["summit_jacket_price_cut"]["amount_ok"]
    assert not s["west_volume_shortfall"]["type_ok"]


def test_reporting_the_decoy_fails(sample_run):
    run = copy.deepcopy(sample_run)
    run["findings"].append({"id": "f4", "summary": "Engineering travel overran by 40%.",
                            "driver_type": "other", "amount_usd": -3200.0,
                            "evidence_query_ids": ["q3"]})
    s = score_run(run, comm_out(sample_run))
    assert not s["checks"]["decoy_ignored"] and s["extra_findings"] == 1


def test_overclaiming_reconciliation_triggers_a_warning(sample_run):
    run = copy.deepcopy(sample_run)
    run["findings"] = [f for f in run["findings"] if f["id"] != "f1"]   # big unexplained gap
    run["summary"] = "All variances were fully reconciled."
    s = score_run(run, None)
    assert any("reconciliation" in w for w in s["warnings"]) and not s["checks"]["reconciliation"]


def test_verifier_catches_every_injected_error(sample_run):
    st = stress_test_verifier(good_commentary(sample_run["facts"]), sample_run)
    assert st["injected"] >= 8
    assert st["caught"] == st["injected"], st["missed"]


def test_live_pipeline_end_to_end_with_a_scripted_model(sample_run):
    findings = sample_run["findings"]
    llm = ScriptedLLM([
        call("revenue_price_volume", month="2026-10-01", group_by="region"),        # q1
        call("revenue_price_volume", month="2026-10-01", group_by="product"),       # q2
        call("run_query", sql="SELECT vendor, budget, actual FROM expenses WHERE "
                              "month = '2026-10-01' AND vendor = 'Brightline Events'"),  # q3
        *[call("record_finding", summary=f["summary"], driver_type=f["driver_type"],
               amount_usd=f["amount_usd"], evidence_query_ids=f["evidence_query_ids"])
          for f in findings],
        say("Done."),
        say(json.dumps(good_commentary(sample_run["facts"]))),                      # narrator
    ])
    [result] = run_live(llm, 1)
    assert result["score"]["pass"], result["score"]