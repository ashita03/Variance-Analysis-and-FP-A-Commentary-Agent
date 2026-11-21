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
            "amount_usd": {"type": "number", "description": "Signed variance impact in dollars."},
            "evidence_query_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["summary", "driver_type", "amount_usd", "evidence_query_ids"]}}},
]