import pytest

from tools import MAX_ROWS, QueryError, Workspace, validate_sql


@pytest.mark.parametrize("sql", [
    "DROP TABLE revenue",
    "SELECT 1; DROP TABLE revenue",
    "INSERT INTO revenue SELECT * FROM revenue",
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM 'secrets.csv'",
    "SELECT * FROM users",
    "PRAGMA database_list",
    "",
])
def test_blocks_unsafe_sql(sql):
    with pytest.raises(QueryError):
        validate_sql(sql)


@pytest.mark.parametrize("sql", [
    "SELECT region, SUM(revenue_actual) FROM revenue GROUP BY region",
    "WITH t AS (SELECT * FROM expenses) SELECT vendor FROM t",
    "SELECT EXTRACT(month FROM month) FROM revenue",
    "SELECT * FROM revenue r JOIN expenses e ON r.month = e.month LIMIT 5;",
])
def test_allows_safe_sql(sql):
    validate_sql(sql)


def test_run_query_returns_citable_id_and_rows():
    ws = Workspace()
    out = ws.run_query("SELECT region, SUM(revenue_actual - revenue_budget) v FROM revenue "
                       "WHERE month = '2026-10-01' GROUP BY region ORDER BY v")
    assert out["query_id"] == "q1" and out["rows"][0][0] == "West"
    assert "q1" in ws.results


def test_row_cap():
    ws = Workspace()
    out = ws.run_query("SELECT * FROM revenue")
    assert len(out["rows"]) == MAX_ROWS and out["truncated"]


def test_database_is_read_only_even_if_validation_were_bypassed():
    ws = Workspace()
    with pytest.raises(Exception):
        ws.con.execute("DELETE FROM revenue")


def test_finding_must_cite_real_evidence():
    ws = Workspace()
    assert "error" in ws.call("record_finding", {"summary": "x", "driver_type": "volume",
                                                 "amount_usd": -1, "evidence_query_ids": ["q99"]})
    assert ws.findings == []


def test_price_volume_reconciles_and_localizes_the_planted_drivers():
    ws = Workspace()
    out = ws.revenue_price_volume("2026-10-01", "total")
    row = dict(zip(out["columns"], out["rows"][0]))
    assert abs(row["variance"] - row["price_effect"] - row["volume_effect"] - row["unexplained"]) < 0.05
    assert abs(row["unexplained"]) < 100                       # rounding only

    def by(group):
        o = ws.revenue_price_volume("2026-10-01", group)
        return {r[0]: dict(zip(o["columns"], r)) for r in o["rows"]}

    assert by("region")["West"]["volume_effect"] < -25000      # planted: West volume shortfall
    assert by("product")["Summit Jacket"]["price_effect"] < -20000   # planted: price cut
    assert abs(by("region")["East"]["price_effect"]) < 10000   # unaffected region stays quiet
    west = by("region")["West"]
    assert west["units_actual"] < west["units_budget"] * 0.9   # units are returned too


def test_price_volume_rejects_bad_group_by():
    assert "error" in Workspace().call("revenue_price_volume",
                                       {"month": "2026-10-01", "group_by": "colour"})