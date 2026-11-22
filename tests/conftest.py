import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def _data():
    os.chdir(ROOT)
    if not (ROOT / "data" / "finance.duckdb").exists():
        subprocess.run([sys.executable, "generate_data.py"], cwd=ROOT, check=True)


@pytest.fixture
def sample_run():
    """A realistic run: real tool outputs on the real synthetic data, built without any LLM."""
    from tools import Workspace
    ws = Workspace()
    region = ws.revenue_price_volume("2026-10-01", "region")      # q1
    product = ws.revenue_price_volume("2026-10-01", "product")    # q2
    bl = ws.run_query("SELECT vendor, budget, actual FROM expenses "
                      "WHERE month = '2026-10-01' AND vendor = 'Brightline Events'")   # q3
    west = next(dict(zip(region["columns"], r)) for r in region["rows"] if r[0] == "West")
    summit = next(dict(zip(product["columns"], r)) for r in product["rows"]
                  if r[0] == "Summit Jacket")
    _, bl_budget, bl_actual = bl["rows"][0]
    ws.record_finding("West volume shortfall.", "volume", round(west["volume_effect"], 2), ["q1"])
    ws.record_finding("Summit Jacket price cut.", "price", round(summit["price_effect"], 2), ["q2"])
    ws.record_finding("Brightline one-time charge.", "one_time",
                      round(-(bl_actual - bl_budget), 2), ["q3"])
    run = {"month": "2026-10-01", "findings": ws.findings, "queries": ws.results}
    run["facts"] = {
        "west_vol": abs(west["volume_effect"]), "summit_price": abs(summit["price_effect"]),
        "bl": bl_actual - bl_budget, "bl_actual": bl_actual, "bl_budget": bl_budget,
        "west_units_act": west["units_actual"], "west_units_bud": west["units_budget"],
        "summit_avg_act": summit["avg_price_actual"],
        "summit_pct": abs((summit["avg_price_actual"] - summit["avg_price_budget"])
                          / summit["avg_price_budget"] * 100),
    }
    run["facts"]["total"] = run["facts"]["west_vol"] + run["facts"]["summit_price"] + run["facts"]["bl"]
    return run


def good_commentary(f: dict) -> dict:
    """Commentary whose every number is real. `f` = sample_run['facts']."""
    return {
        "headline": (f"October profit was hit by ${f['total']:,.0f} across three drivers: "
                     f"${f['bl']:,.0f} of event spend, a ${f['west_vol']:,.0f} West volume "
                     f"shortfall and a ${f['summit_price']:,.0f} Summit Jacket price cut."),
        "paragraphs": [
            {"finding_ids": ["f1"], "text":
                f"West sold {f['west_units_act']:,} units against {f['west_units_bud']:,} "
                f"budgeted, a ${f['west_vol']:,.0f} unfavorable volume effect."},
            {"finding_ids": ["f2"], "text":
                f"Summit Jacket's average price fell {f['summit_pct']:.1f}% to "
                f"${f['summit_avg_act']:.2f}, costing ${f['summit_price']:,.0f}."},
            {"finding_ids": ["f3"], "text":
                f"Brightline Events cost ${f['bl_actual']:,.0f} against a "
                f"${f['bl_budget']:,.0f} budget."},
        ]}