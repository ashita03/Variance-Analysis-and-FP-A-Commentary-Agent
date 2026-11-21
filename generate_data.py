"""Synthetic finance data for the variance-analysis agent project.

24 months of budget vs. actuals for a fictional outdoor-gear company.
In the target month (2026-10) we PLANT three drivers and one decoy.
The answer key is saved to data/ground_truth.json so we can evaluate
the agent later: did it find what we hid?
"""
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

SEED = 42
TARGET = pd.Timestamp("2026-10-01")
rng = np.random.default_rng(SEED)

MONTHS = pd.date_range("2024-11-01", "2026-10-01", freq="MS")
REGION_SHARE = {"East": 0.32, "West": 0.28, "Central": 0.17, "South": 0.23}
PRODUCTS = {  # product: (company-wide monthly units, list price)
    "Trail Pack": (1800, 85.0),
    "Summit Jacket": (900, 220.0),
    "Basecamp Tent": (450, 340.0),
}
SEASON = {1: .85, 2: .85, 3: .95, 4: 1.05, 5: 1.15, 6: 1.2,
          7: 1.15, 8: 1.1, 9: 1.0, 10: .95, 11: 1.1, 12: 1.2}

# department, category, vendor, monthly budget
EXPENSE_LINES = [
    ("Sales", "Salaries", "Payroll", 140000),
    ("Sales", "Travel", "Various", 9000),
    ("Marketing", "Advertising", "AdReach", 45000),
    ("Marketing", "Events", "Brightline Events", 6000),
    ("Marketing", "Software", "MarTechCo", 4000),
    ("Engineering", "Salaries", "Payroll", 210000),
    ("Engineering", "Software", "CloudHost", 18000),
    ("Engineering", "Travel", "Various", 8000),
    ("G&A", "Salaries", "Payroll", 90000),
    ("G&A", "Rent", "Harbor Properties", 25000),
    ("G&A", "Software", "OfficeSuite", 7000),
    ("Support", "Salaries", "Payroll", 70000),
    ("Support", "Software", "HelpDeskCo", 5000),
]


def build_revenue() -> pd.DataFrame:
    rows = []
    for i, m in enumerate(MONTHS):
        growth = 1 + 0.008 * i
        for region, share in REGION_SHARE.items():
            for product, (units, price) in PRODUCTS.items():
                u_bud = round(units * share * SEASON[m.month] * growth)
                p_bud = price
                u_act = u_bud * (1 + rng.normal(0, 0.03))
                p_act = p_bud * (1 + rng.normal(0, 0.004))

                # PLANTED DRIVER 1: West volume shortfall (mild in Sep, severe in Oct)
                if region == "West" and m >= pd.Timestamp("2026-09-01"):
                    u_act *= 0.93 if m.month == 9 else 0.80
                # PLANTED DRIVER 2: Summit Jacket price cut (12%) from Sep, small volume lift
                if product == "Summit Jacket" and m >= pd.Timestamp("2026-09-01"):
                    p_act = p_bud * 0.88
                    u_act *= 1.04

                u_act = round(u_act)
                rows.append(dict(
                    month=m, region=region, product=product,
                    units_budget=u_bud, units_actual=u_act,
                    price_budget=round(p_bud, 2), price_actual=round(p_act, 2),
                    revenue_budget=round(u_bud * p_bud, 2),
                    revenue_actual=round(u_act * p_act, 2),
                ))
    return pd.DataFrame(rows)


def build_expenses() -> pd.DataFrame:
    rows = []
    for i, m in enumerate(MONTHS):
        for dept, cat, vendor, base in EXPENSE_LINES:
            bud = base * (1 + 0.004 * i)
            act = bud * (1 + rng.normal(0, 0.02))
            # PLANTED DRIVER 3: one-time, unbudgeted $85K event charge
            if vendor == "Brightline Events" and m == TARGET:
                act += 85000
            # DECOY: +40% but only $3.2K, below materiality. Should NOT be reported.
            if dept == "Engineering" and cat == "Travel" and m == TARGET:
                act = bud + 3200
            rows.append(dict(month=m, department=dept, category=cat, vendor=vendor,
                             budget=round(bud, 2), actual=round(act, 2)))
    return pd.DataFrame(rows)


GROUND_TRUTH = {
    "target_month": "2026-10",
    "drivers": [
        {"id": "west_volume_shortfall", "type": "volume",
         "where": {"region": "West"},
         "description": "West region unit volume ~20% below budget in Oct (7% in Sep), all products."},
        {"id": "summit_jacket_price_cut", "type": "price",
         "where": {"product": "Summit Jacket"},
         "description": "Summit Jacket average price 12% below budget from Sep, with only a 4% volume lift."},
        {"id": "brightline_one_time_charge", "type": "one_time_expense",
         "where": {"department": "Marketing", "vendor": "Brightline Events"},
         "amount": 85000,
         "description": "Unbudgeted one-time $85K charge from Brightline Events."},
    ],
    "decoys": [
        {"id": "engineering_travel_overrun",
         "note": "+40% vs budget but only $3.2K. Below materiality; must NOT be reported."},
    ],
}


if __name__ == "__main__":
    Path("data").mkdir(exist_ok=True)
    revenue, expenses = build_revenue(), build_expenses()
    con = duckdb.connect("data/finance.duckdb")
    con.register("rev_df", revenue)
    con.register("exp_df", expenses)
    con.execute("CREATE OR REPLACE TABLE revenue AS SELECT * FROM rev_df")
    con.execute("CREATE OR REPLACE TABLE expenses AS SELECT * FROM exp_df")
    con.close()
    Path("data/ground_truth.json").write_text(json.dumps(GROUND_TRUTH, indent=2))
    print(f"revenue rows: {len(revenue)}, expense rows: {len(expenses)}")
    print("wrote data/finance.duckdb and data/ground_truth.json")