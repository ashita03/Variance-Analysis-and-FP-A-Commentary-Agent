"""Tools the investigator agent can call, plus the safety layer around them.

Safety is layered, because a model-written SQL string is untrusted input:
  1. The DB connection is read-only with external file access disabled (the real guard).
  2. validate_sql() allows one SELECT/WITH statement on known tables only.
  3. Results are row-capped so one query can't flood the model's context.
"""
import re

import duckdb

from variance import DB_PATH, get_variances

ALLOWED_TABLES = {"revenue", "expenses"}
MAX_ROWS = 100
BLOCKED = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|pragma|export"
    r"|import|install|load|call|set|read_\w+|glob)\b", re.I)


class QueryError(ValueError):
    pass


def validate_sql(sql: str) -> str:
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        raise QueryError("Empty query.")
    if ";" in s:
        raise QueryError("Only one statement is allowed.")
    if not re.match(r"(?is)^(select|with)\b", s):
        raise QueryError("Only SELECT queries are allowed.")
    if m := BLOCKED.search(s):
        raise QueryError(f"Forbidden keyword: {m.group(0)}")
    if re.search(r"(?i)\b(from|join)\s+['\"]", s):
        raise QueryError("Use bare table names (revenue, expenses), not quoted names or files.")
    scan = re.sub(r"(?is)\b(extract|substring|trim)\s*\([^)]*\)", "", s)  # FROM inside functions
    ctes = {c.lower() for c in re.findall(r"(?i)\b(\w+)\s+as\s*\(", s)}
    tables = {t.lower() for t in re.findall(r"(?i)\b(?:from|join)\s+([a-z_]\w*)", scan)}
    unknown = tables - ALLOWED_TABLES - ctes
    if unknown:
        raise QueryError(f"Unknown table(s) {sorted(unknown)}. Available: revenue, expenses.")
    return s


class Workspace:
    """State for ONE investigation: DB connection, citable query results, findings."""

    def __init__(self, db_path: str = DB_PATH):
        self.con = duckdb.connect(db_path, read_only=True,
                                  config={"enable_external_access": False})
        self.results: dict[str, dict] = {}
        self.findings: list[dict] = []

    def _store(self, sql, columns, rows, truncated=False) -> str:
        qid = f"q{len(self.results) + 1}"
        self.results[qid] = {"id": qid, "sql": sql, "columns": columns,
                             "rows": rows, "truncated": truncated}
        return qid

    # ---- tool 1: the deterministic detector, exposed to the agent
    def get_material_variances(self, month: str) -> dict:
        if not re.fullmatch(r"\d{4}-\d{2}-01", month or ""):
            raise QueryError("month must look like 2026-10-01 (first of the month).")
        rows = get_variances(self.con, month)
        qid = self._store(f"get_material_variances({month})", None, rows)
        return {"query_id": qid, "material_variances": rows}

    # ---- tool 1b: deterministic price/volume decomposition (no need for the model to derive it)
    PV_GROUPS = {"total": [], "region": ["region"], "product": ["product"],
                 "region+product": ["region", "product"]}

    def revenue_price_volume(self, month: str, group_by: str = "total") -> dict:
        if not re.fullmatch(r"\d{4}-\d{2}-01", month or ""):
            raise QueryError("month must look like 2026-10-01 (first of the month).")
        if group_by not in self.PV_GROUPS:
            raise QueryError(f"group_by must be one of {sorted(self.PV_GROUPS)}")
        dims = self.PV_GROUPS[group_by]
        dim_sql = ", ".join(dims)
        select = f"{dim_sql}, " if dims else ""
        group = f"GROUP BY {dim_sql} ORDER BY {dim_sql}" if dims else ""
        sql = (f"SELECT {select}SUM(revenue_budget) AS budget_revenue, "
               "SUM(revenue_actual) AS actual_revenue, "
               "SUM(units_budget) AS units_budget, SUM(units_actual) AS units_actual, "
               "SUM(revenue_budget) / SUM(units_budget) AS avg_price_budget, "
               "SUM(revenue_actual) / SUM(units_actual) AS avg_price_actual, "
               "SUM(revenue_actual - revenue_budget) AS variance, "
               "SUM((price_actual - price_budget) * units_actual) AS price_effect, "
               "SUM((units_actual - units_budget) * price_budget) AS volume_effect "
               f"FROM revenue WHERE month = CAST(? AS DATE) {group}")
        cur = self.con.execute(sql, [month])
        columns = [d[0] for d in cur.description] + ["unexplained"]
        rows = []
        for r in cur.fetchall():
            r = [round(v, 2) if isinstance(v, float) else v for v in r]
            rows.append(r + [round(r[-3] - r[-2] - r[-1], 2)])   # variance - price - volume
        qid = self._store(f"revenue_price_volume({month}, {group_by})", columns, rows)
        return {"query_id": qid, "columns": columns, "rows": rows}

    # ---- tool 2: free-form read-only SQL
    def run_query(self, sql: str) -> dict:
        s = validate_sql(sql)
        cur = self.con.execute(f"SELECT * FROM ({s}) LIMIT {MAX_ROWS + 1}")
        columns = [d[0] for d in cur.description]
        rows = [[round(v, 2) if isinstance(v, float) else v for v in r]
                for r in cur.fetchall()]
        truncated = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]
        qid = self._store(s, columns, rows, truncated)
        return {"query_id": qid, "columns": columns, "rows": rows, "truncated": truncated}

    # ---- tool 3: record a root-cause finding, which must cite stored evidence
    def record_finding(self, summary: str, driver_type: str, amount_usd: float,
                       evidence_query_ids: list) -> dict:
        if not summary or not summary.strip():
            raise QueryError("summary is required.")
        missing = [q for q in evidence_query_ids or [] if q not in self.results]
        if not evidence_query_ids or missing:
            raise QueryError(f"evidence_query_ids must be existing query ids. Unknown: {missing}")
        fid = f"f{len(self.findings) + 1}"
        self.findings.append({"id": fid, "summary": summary.strip(),
                              "driver_type": driver_type, "amount_usd": float(amount_usd),
                              "evidence_query_ids": list(evidence_query_ids)})
        return {"recorded": fid}

    def call(self, name: str, args: dict) -> dict:
        """Dispatch a tool call. Errors go BACK to the model so it can self-correct."""
        fn = {"get_material_variances": self.get_material_variances,
              "revenue_price_volume": self.revenue_price_volume,
              "run_query": self.run_query,
              "record_finding": self.record_finding}.get(name)
        if fn is None:
            return {"error": f"Unknown tool '{name}'."}
        try:
            return fn(**args)
        except (QueryError, TypeError, ValueError, duckdb.Error) as e:
            return {"error": f"{type(e).__name__}: {e}"}


TOOLS = [
    {"type": "function", "function": {
        "name": "get_material_variances",
        "description": "List all material budget-vs-actual variances (>=5% and >=$10K) for a month, "
                       "at total, region, product, department, category and vendor level.",
        "parameters": {"type": "object", "properties": {
            "month": {"type": "string", "description": "First day of month, e.g. 2026-10-01"}},
            "required": ["month"]}}},
    {"type": "function", "function": {
        "name": "revenue_price_volume",
        "description": "Split the revenue variance for a month into PRICE effect and VOLUME effect, "
                       "grouped by total, region, product or region+product. Effects reconcile to "
                       "the variance (tiny 'unexplained' = rounding). Also returns units and average prices "
                       "(averages are mix-affected unless grouped by product). Call it for prior "
                       "months too to see when a change started.",
        "parameters": {"type": "object", "properties": {
            "month": {"type": "string", "description": "First day of month, e.g. 2026-10-01"},
            "group_by": {"type": "string",
                         "enum": ["total", "region", "product", "region+product"]}},
            "required": ["month", "group_by"]}}},
    {"type": "function", "function": {
        "name": "run_query",
        "description": "Run ONE read-only DuckDB SELECT on tables revenue and expenses. "
                       f"Returns at most {MAX_ROWS} rows and a query_id you can cite as evidence.",
        "parameters": {"type": "object", "properties": {
            "sql": {"type": "string"}}, "required": ["sql"]}}},
    {"type": "function", "function": {
        "name": "record_finding",
        "description": "Record one distinct ROOT CAUSE with its dollar impact and the query ids proving it.",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string", "description": "One or two sentences with specific figures."},
            "driver_type": {"type": "string",
                            "enum": ["volume", "price", "mix", "one_time", "timing", "other"]},
            "amount_usd": {"type": "number",
                           "description": "Impact on PROFIT in dollars. Negative = unfavorable "
                                          "(revenue shortfall OR cost overrun), positive = favorable."},
            "evidence_query_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["summary", "driver_type", "amount_usd", "evidence_query_ids"]}}},
]