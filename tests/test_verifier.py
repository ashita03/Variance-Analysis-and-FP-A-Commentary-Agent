import copy

from conftest import good_commentary
from verifier import extract_numbers, verify_commentary, verify_findings


def test_extract_numbers_handles_money_percent_suffix_and_skips_noise():
    text = ("Revenue fell $26,215.20 (12.0%) to $193.60; West sold 786 vs 992 units in "
            "Oct 2026 (2026-10-01). We saw 3 drivers, about $85K, see f2.")
    got = extract_numbers(text)
    assert [c["text"] for c in got] == ["$26,215.20", "12.0%", "$193.60", "786", "992", "$85K"]
    k = got[-1]
    assert k["scale"] == 1000 and got[1]["is_pct"] and got[0]["decimals"] == 2


def test_correct_commentary_passes_including_derived_numbers(sample_run):
    # includes: a within-row percent change (12.0%), a total of findings, direct cells
    report = verify_commentary(good_commentary(sample_run["facts"]), sample_run)
    assert report["ok"], report
    assert report["checked"] >= 8


def test_rounding_to_thousands_is_accepted(sample_run):
    c = good_commentary(sample_run["facts"])
    k = round(sample_run["facts"]["bl"] / 1000)
    c["paragraphs"][2]["text"] = f"Brightline Events was an unbudgeted ${k}K charge."
    assert verify_commentary(c, sample_run)["ok"]


def test_a_number_not_in_the_evidence_is_flagged(sample_run):
    c = good_commentary(sample_run["facts"])
    c["paragraphs"][1]["text"] = "Summit Jacket's price cut cost $26,315 in October."
    report = verify_commentary(c, sample_run)
    assert not report["ok"]
    assert [u["text"] for u in report["unsupported"]] == ["$26,315"]
    assert report["unsupported"][0]["where"] == "paragraph 2"


def test_evidence_is_scoped_to_the_findings_a_paragraph_cites(sample_run):
    c = good_commentary(sample_run["facts"])
    bl = f"{sample_run['facts']['bl']:,.0f}"           # real number, but evidence of f3 only
    c["paragraphs"][0]["text"] = f"West lost ${bl} of volume."      # paragraph cites f1
    report = verify_commentary(c, sample_run)
    assert not report["ok"] and report["unsupported"][0]["where"] == "paragraph 1"


def test_uncovered_finding_and_unknown_id_are_reported(sample_run):
    c = good_commentary(sample_run["facts"])
    c["paragraphs"] = c["paragraphs"][:2]
    c["paragraphs"][0]["finding_ids"] = ["f1", "f99"]
    issues = verify_commentary(c, sample_run)["issues"]
    assert any("f3" in i and "not covered" in i for i in issues)
    assert any("f99" in i for i in issues)


def test_malformed_commentary_is_rejected(sample_run):
    for bad in (None, {"headline": 5}, {"headline": "x", "paragraphs": []}, "text"):
        assert not verify_commentary(bad, sample_run)["ok"]


def test_findings_are_checked_against_their_own_evidence(sample_run):
    assert all(r["ok"] for r in verify_findings(sample_run))
    broken = copy.deepcopy(sample_run)
    broken["findings"][1]["amount_usd"] = -26315.0               # wrong amount
    broken["findings"][0]["summary"] = "West fell short by $99,999."   # wrong number in text
    res = {r["finding"]: r for r in verify_findings(broken)}
    assert not res["f2"]["amount_ok"] and not res["f1"]["ok"] and res["f3"]["ok"]