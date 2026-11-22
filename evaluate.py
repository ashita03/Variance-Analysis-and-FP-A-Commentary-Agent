"""Evaluation: score pipeline runs against the planted answer key, and stress-test the verifier.

    python evaluate.py            # score runs/latest.json + runs/commentary.json
    python evaluate.py --live 3   # run the full pipeline 3 times (uses your LLM quota) and score each
"""
import argparse
import copy
import json
from pathlib import Path

from tools import Workspace
from verifier import extract_numbers, verify_commentary, verify_findings

TYPE_MAP = {"volume": "volume", "price": "price", "one_time_expense": "one_time"}
REVENUE_TYPES, EXPENSE_TYPES = {"volume", "price", "mix"}, {"one_time", "timing", "other"}
AMOUNT_TOL_PCT, AMOUNT_TOL_ABS, RECON_TOL = 0.01, 100.0, 0.20


def oracle(month: str) -> dict:
    """True dollar impacts on PROFIT, computed straight from the data (not from the agent)."""
    ws = Workspace()

    def rev(group, key=None):
        o = ws.revenue_price_volume(month, group)
        rows = [dict(zip(o["columns"], r)) for r in o["rows"]]
        return rows[0] if key is None else next(r for r in rows if r[group] == key)

    def exp_var(extra=""):
        return ws.con.execute("SELECT SUM(actual - budget) FROM expenses "
                              f"WHERE month = CAST(? AS DATE){extra}", [month]).fetchone()[0]

    return {"amounts": {"west_volume_shortfall": rev("region", "West")["volume_effect"],
                        "summit_jacket_price_cut": rev("product", "Summit Jacket")["price_effect"],
                        "brightline_one_time_charge": -exp_var(" AND vendor = 'Brightline Events'")},
            "revenue_total": rev("total")["variance"], "expense_total": -exp_var()}


def _recon(findings, types, total):
    got = sum(f["amount_usd"] for f in findings if f["driver_type"] in types)
    resid = total - got
    pct = abs(resid) / abs(total) if total else 0.0
    return {"total": round(total, 2), "explained": round(got, 2), "residual": round(resid, 2),
            "residual_pct": round(pct, 3), "ok": pct <= RECON_TOL}


def score_run(run: dict, comm_out: dict | None, truth: dict | None = None) -> dict:
    truth = truth or json.loads(Path("data/ground_truth.json").read_text())
    orc, findings, warnings = oracle(run["month"]), run["findings"], []
    checks, drivers, matched = {}, [], set()

    for d in truth["drivers"]:
        want = TYPE_MAP[d["type"]]
        key = str(list(d["where"].values())[-1]).lower().split()[0]
        cands = [f for f in findings if key in f["summary"].lower()]
        exp = orc["amounts"][d["id"]]
        f = min([c for c in cands if c["driver_type"] == want] or cands,
                key=lambda c: abs(c["amount_usd"] - exp), default=None)
        found = f is not None
        type_ok = found and f["driver_type"] == want
        amount_ok = found and abs(f["amount_usd"] - exp) <= max(AMOUNT_TOL_ABS,
                                                                AMOUNT_TOL_PCT * abs(exp))
        timing = None if want == "one_time" else bool(found and "september" in f["summary"].lower())
        if found:
            matched.add(f["id"])
        drivers.append({"id": d["id"], "found": found, "type_ok": type_ok,
                        "amount_ok": amount_ok, "timing_noted": timing,
                        "expected": round(exp, 2), "got": f["amount_usd"] if found else None})
        checks[d["id"]] = bool(found and type_ok and amount_ok)

    text = " ".join(f["summary"].lower() for f in findings)
    checks["decoy_ignored"] = not ("travel" in text and "engineering" in text)
    checks["findings_verified"] = all(r["ok"] for r in verify_findings(run))
    commentary = (comm_out or {}).get("commentary")
    checks["commentary_verified"] = bool(commentary and verify_commentary(commentary, run)["ok"])

    recon = {"revenue": _recon(findings, REVENUE_TYPES, orc["revenue_total"]),
             "expenses": _recon(findings, EXPENSE_TYPES, orc["expense_total"])}
    checks["reconciliation"] = all(r["ok"] for r in recon.values())
    if "reconcil" in (run.get("summary") or "").lower():
        for name, r in recon.items():
            if r["residual_pct"] > 0.05:
                warnings.append(f"Summary claims reconciliation, but {name} findings leave "
                                f"{r['residual_pct']:.0%} (${abs(r['residual']):,.0f}) unexplained.")
    passed = sum(checks.values())
    return {"checks": checks, "drivers": drivers, "reconciliation": recon,
            "extra_findings": len([f for f in findings if f["id"] not in matched]),
            "warnings": warnings, "passed": passed, "total": len(checks),
            "pass": passed == len(checks)}


def _mutate(text: str, claim: dict) -> str:
    """Corrupt one number by ~3% (at least one unit of its last digit), keeping its style."""
    raw, d, v = claim["text"], claim["decimals"], float(claim["value"])
    new = v * 1.03 if abs(v * 0.03) >= 10 ** -d else v + 10 ** -d
    num = f"{new:,.{d}f}" if "," in raw else f"{new:.{d}f}"
    cur = "$" if raw.startswith("$") else ""
    suf = raw[-1] if raw[-1] in "%KkMm" else ""
    s, e = claim["span"]
    return text[:s] + cur + num + suf + text[e:]


def stress_test_verifier(commentary: dict, run: dict) -> dict:
    """Corrupt each number in turn and check that the verifier notices."""
    injected, missed = 0, []
    spots = [("headline", None, commentary["headline"])] + [
        ("paragraph", i, p["text"]) for i, p in enumerate(commentary["paragraphs"])]
    for kind, i, text in spots:
        for claim in extract_numbers(text):
            bad = copy.deepcopy(commentary)
            new_text = _mutate(text, claim)
            if kind == "headline":
                bad["headline"] = new_text
            else:
                bad["paragraphs"][i]["text"] = new_text
            injected += 1
            if verify_commentary(bad, run)["ok"]:
                missed.append(f"{kind} {'' if i is None else i + 1}: {claim['text']}".strip())
    return {"injected": injected, "caught": injected - len(missed), "missed": missed}


def usd(x: float) -> str:
    return f"-${abs(x):,.0f}" if x < 0 else f"${x:,.0f}"


def print_score(score: dict) -> None:
    print(f"\nEVAL: {score['passed']}/{score['total']} checks passed\n")
    for d in score["drivers"]:
        ok = score["checks"][d["id"]]
        timing = {True: ", start date noted", False: ", start date NOT noted", None: ""}[d["timing_noted"]]
        got = ("not found" if not d["found"]
               else f"{usd(d['got'])} vs expected {usd(d['expected'])}")
        print(f"  {'✓' if ok else '✗'} {d['id']:<28} type_ok={d['type_ok']}  {got}{timing}")
    for name, label in [("decoy_ignored", "decoy ignored"),
                        ("findings_verified", "findings traced to their evidence"),
                        ("commentary_verified", "commentary traced to evidence")]:
        print(f"  {'✓' if score['checks'][name] else '✗'} {label}")
    for name, r in score["reconciliation"].items():
        print(f"  {'✓' if r['ok'] else '✗'} reconciliation ({name}): findings explain "
              f"{usd(r['explained'])} of {usd(r['total'])}  (residual {r['residual_pct']:.0%})")
    print(f"  extra findings beyond the planted drivers: {score['extra_findings']}")
    for w in score["warnings"]:
        print(f"  ⚠ {w}")


def run_live(llm, n: int, month: str = "2026-10-01", on_event=lambda *a, **k: None) -> list:
    from investigator import investigate
    from narrator import write_commentary
    results = []
    for _ in range(n):
        run = investigate(llm, month, on_event=on_event)
        comm = write_commentary(llm, run, on_event=on_event)
        results.append({"run": run, "commentary": comm, "score": score_run(run, comm)})
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", type=int, metavar="N", help="run the full pipeline N times and score each")
    args = ap.parse_args()

    if args.live:
        from llm import LLM
        print(f"Running the full pipeline {args.live} time(s). This uses your LLM quota ...")
        results = run_live(LLM(), args.live)
        for i, r in enumerate(results, 1):
            print(f"\n=== run {i} ===")
            print_score(r["score"])
        n = len(results)
        print(f"\nPASS RATE: {sum(r['score']['pass'] for r in results)}/{n} runs passed all checks")
        names = results[0]["score"]["checks"]
        for name in names:
            print(f"  {name:<30}{sum(r['score']['checks'][name] for r in results)}/{n}")
        Path("runs").mkdir(exist_ok=True)
        Path("runs/eval_report.json").write_text(json.dumps(
            [r["score"] for r in results], indent=2, default=str))
        print("\nSaved runs/eval_report.json")
    else:
        try:
            run = json.loads(Path("runs/latest.json").read_text())
            comm = json.loads(Path("runs/commentary.json").read_text())
        except FileNotFoundError:
            raise SystemExit("Run `python investigator.py` and `python narrator.py` first.")
        print_score(score := score_run(run, comm))
        if comm.get("commentary"):
            st = stress_test_verifier(comm["commentary"], run)
            print(f"\nVERIFIER STRESS TEST: caught {st['caught']}/{st['injected']} injected errors")
            for m in st["missed"]:
                print(f"  missed: {m}")