"""Regression: the verifier must stay strict when the evidence is large and numerically dense
(many rows x many columns), which is what real agent runs produce."""
import pytest

from evaluate import stress_test_verifier
from verifier import verify_commentary

M = "2026-10-01"


@pytest.fixture
def rich_run():
    from tools import Workspace
    ws = Workspace()
    rows = lambda o: [dict(zip(o["columns"], r)) for r in o["rows"]]
    pv_region = ws.revenue_price_volume(M, "region")                       # q1
    pv_product = ws.revenue_price_volume(M, "product")                     # q2
    ws.revenue_price_volume(M, "region+product")                           # q3 (12 rows x 12 cols)
    ws.run_query("SELECT region, product, units_budget, units_actual, price_budget, "
                 "price_actual, revenue_budget, revenue_actual FROM revenue "
                 f"WHERE month = '{M}' ORDER BY region, product")          # q4
    hist = ws.run_query("SELECT month, budget, actual FROM expenses "
                        "WHERE vendor = 'Brightline Events' ORDER BY month")   # q5 (23 rows)
    ws.run_query("SELECT product, SUM(units_budget) AS ub, SUM(units_actual) AS ua, "
                 "AVG(price_budget) AS pb, AVG(price_actual) AS pa FROM revenue "
                 f"WHERE month = '{M}' GROUP BY product")                  # q6 (abbreviated names)
    west = next(r for r in rows(pv_region) if r["region"] == "West")
    summit = next(r for r in rows(pv_product) if r["product"] == "Summit Jacket")
    _, bud, act = hist["rows"][-1]
    prior = [r[2] for r in hist["rows"][:-1]]
    ws.record_finding("Brightline.", "one_time", round(-(act - bud), 2), ["q5"])
    ws.record_finding("Summit price.", "price", round(summit["price_effect"], 2),
                      ["q2", "q3", "q4", "q6"])
    ws.record_finding("West volume.", "volume", round(west["volume_effect"], 2),
                      ["q1", "q3", "q4"])
    pct = abs((summit["avg_price_actual"] - summit["avg_price_budget"])
              / summit["avg_price_budget"] * 100)
    commentary = {
        "headline": "October was hurt by event spend, a Summit Jacket price cut and a West "
                    "volume shortfall.",
        "paragraphs": [
            {"finding_ids": ["f1"], "text":
                f"Brightline Events had an unfavorable variance of -${act - bud:,.2f}, with "
                f"actual spend of ${act:,.2f} against a budget of ${bud:,.2f}. Monthly costs "
                f"had tracked between ${min(prior):,.2f} and ${max(prior):,.2f} before that."},
            {"finding_ids": ["f2"], "text":
                f"Summit Jacket had an unfavorable price variance of "
                f"-${abs(summit['price_effect']):,.2f} across {summit['units_actual']:,} units. "
                f"The average price was ${summit['avg_price_actual']:.2f} versus a budgeted "
                f"${summit['avg_price_budget']:.2f}, a {pct:.0f}% price reduction."},
            {"finding_ids": ["f3"], "text":
                f"West had an unfavorable volume variance of -${abs(west['volume_effect']):,.2f} "
                f"as units reached {west['units_actual']:,} against a budget of "
                f"{west['units_budget']:,}. This shortfall of "
                f"{west['units_budget'] - west['units_actual']:,} units hit all three products."},
        ]}
    return {"month": M, "findings": ws.findings, "queries": ws.results, "commentary": commentary}


def test_the_honest_commentary_verifies(rich_run):
    assert verify_commentary(rich_run["commentary"], rich_run)["ok"]


def test_stress_test_stays_strict_on_dense_evidence(rich_run):
    st = stress_test_verifier(rich_run["commentary"], rich_run)
    assert st["injected"] >= 12
    assert st["missed"] == [], st["missed"]