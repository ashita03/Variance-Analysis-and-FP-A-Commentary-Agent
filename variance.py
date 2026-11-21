"""Deterministic variance detection. No LLM involved.

Computes actual-vs-budget at several levels of detail and keeps only
variances that are material: BOTH >= min_pct AND >= min_abs dollars.
This is the "what's worth investigating?" step; the agent handles "why?".
"""
import duckdb

DB_PATH = "data/finance.duckdb"

# (table, label, group-by columns). All values are constants, never user input.
SLICES = [
    ("revenue", "total", []),
    ("revenue", "by region", ["region"]),
    ("revenue", "by product", ["product"]),
    ("expenses", "total", []),
    ("expenses", "by department", ["department"]),
    ("expenses", "by department+category", ["department", "category"]),
    ("expenses", "by department+vendor", ["department", "vendor"]),
]
COLS = {"revenue": ("revenue_actual", "revenue_budget"), "expenses": ("actual", "budget")}


def connect(read_only: bool = True) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(DB_PATH, read_only=read_only)


def get_variances(con, month: str, min_pct: float = 5.0, min_abs: float = 10_000):
    """Return material variances for `month` (e.g. '2026-10-01'), largest first."""
    results = []
    for table, label, dims in SLICES:
        a, b = COLS[table]
        dim_sql = ", ".join(dims)
        select = f"{dim_sql}, " if dims else ""
        group = f"GROUP BY {dim_sql}" if dims else ""
        sql = (f"SELECT {select}SUM({a}) AS actual, SUM({b}) AS budget "
               f"FROM {table} WHERE month = CAST(? AS DATE) {group}")
        for row in con.execute(sql, [month]).fetchall():
            *keys, actual, budget = row
            var = actual - budget
            pct = var / budget * 100 if budget else 0.0
            if abs(var) >= min_abs and abs(pct) >= min_pct:
                # revenue above budget is good; expenses above budget are bad
                favorable = var > 0 if table == "revenue" else var < 0
                results.append({
                    "table": table, "slice": label, "keys": dict(zip(dims, keys)),
                    "actual": round(actual, 2), "budget": round(budget, 2),
                    "variance": round(var, 2), "variance_pct": round(pct, 1),
                    "favorable": favorable,
                })
    return sorted(results, key=lambda r: -abs(r["variance"]))


if __name__ == "__main__":
    con = connect()
    found = get_variances(con, "2026-10-01")
    print(f"{len(found)} material variances in 2026-10 (>=5% and >=$10K)\n")
    for r in found:
        where = ", ".join(f"{k}={v}" for k, v in r["keys"].items()) or "TOTAL"
        flag = "fav  " if r["favorable"] else "UNFAV"
        print(f"{flag} {r['table']:<9}{r['slice']:<24}{where:<42}"
              f"{r['variance']:>12,.0f}  ({r['variance_pct']:+.1f}%)")