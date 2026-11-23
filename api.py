"""Web API: serves the saved (cached) run plus everything the UI needs to show the evidence."""
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from evaluate import EXPENSE_TYPES, REVENUE_TYPES, _recon, oracle
from variance import connect, get_variances
from verifier import verify_commentary, verify_findings

app = FastAPI(title="Variance Analysis Agent")


def _table(q: dict) -> dict:
    rows = q["rows"][:30]
    if rows and isinstance(rows[0], dict):                       # get_material_variances rows
        cols = ["slice", "where", "actual", "budget", "variance", "variance_pct"]
        rows = [[r["table"] + " " + r["slice"], ", ".join(f"{k}={v}" for k, v in r["keys"].items())
                 or "total", r["actual"], r["budget"], r["variance"], r["variance_pct"]] for r in rows]
        return {"sql": q["sql"], "columns": cols, "rows": rows}
    return {"sql": q["sql"], "columns": q["columns"], "rows": rows}


@app.get("/api/run")
def get_run():
    try:
        run = json.loads(Path("runs/latest.json").read_text())
        comm = json.loads(Path("runs/commentary.json").read_text())
    except FileNotFoundError:
        raise HTTPException(404, "No saved run yet. Run investigator.py, then narrator.py.")
    commentary = comm["commentary"]
    orc = oracle(run["month"])
    evidence = sorted({q for f in run["findings"] for q in f["evidence_query_ids"]})
    con = connect()
    try:
        variances = get_variances(con, run["month"])
        q = "SELECT SUM({a}), SUM({b}) FROM {t} WHERE month = CAST(? AS DATE)"
        rev = con.execute(q.format(a="revenue_actual", b="revenue_budget", t="revenue"), [run["month"]]).fetchone()
        exp = con.execute(q.format(a="actual", b="budget", t="expenses"), [run["month"]]).fetchone()
    finally:
        con.close()
    return {
        "month": run["month"], "findings": run["findings"], "commentary": commentary,
        "verification": verify_commentary(commentary, run),
        "finding_checks": verify_findings(run),
        "reconciliation": {"revenue": _recon(run["findings"], REVENUE_TYPES, orc["revenue_total"]),
                           "expenses": _recon(run["findings"], EXPENSE_TYPES, orc["expense_total"])},
        "evidence": {q: _table(run["queries"][q]) for q in evidence},
        "variances": variances[:12],
        "totals": {"revenue": {"actual": rev[0], "budget": rev[1]},
                   "expenses": {"actual": exp[0], "budget": exp[1]}},
    }


@app.get("/api/health")
def health():
    return {"ok": True}


app.mount("/", StaticFiles(directory="static", html=True), name="static")