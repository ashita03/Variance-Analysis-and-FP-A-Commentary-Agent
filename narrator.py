"""Narrator agent: writes management commentary, then a generate -> verify -> revise loop."""
import json
import re
from pathlib import Path

from verifier import verify_commentary, verify_findings

NARRATOR_PROMPT = """You are an FP&A analyst writing management commentary for a monthly \
budget-vs-actual review. You are given findings and the query results that prove them.
Write for a CFO: plain, specific, concise.

RULES
- Use ONLY numbers that appear in the data provided (or a simple difference or percentage of
  them). Never estimate or invent a figure.
- Cover every finding with one paragraph of 2-3 sentences: what happened, how big in dollars,
  the driver, and when it started if the data shows it.
- amount_usd is the impact on PROFIT: negative = unfavorable.
- Plain text only: no markdown, no bullet points.
- Do not claim causes the data cannot support (for example, never guess WHY an event was held).

Reply with ONLY a JSON object, no code fences:
{"headline": "<one sentence on the overall picture>",
 "paragraphs": [{"finding_ids": ["f1"], "text": "..."}]}"""


def _facts(run: dict, max_rows: int = 30) -> str:
    ev = sorted({q for f in run["findings"] for q in f["evidence_query_ids"]},
                key=lambda q: int(q[1:]))
    pack = {"month": run["month"], "findings": run["findings"],
            "evidence": [{"id": q, "sql": run["queries"][q]["sql"],
                          "columns": run["queries"][q]["columns"],
                          "rows": run["queries"][q]["rows"][:max_rows]} for q in ev]}
    return json.dumps(pack, default=str)


def _parse(text: str):
    t = (text or "").strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found")
    return json.loads(t[start:end + 1])


def _feedback(report: dict) -> str:
    lines = ["Automatic verification found problems. Fix them and reply with the full "
             "corrected JSON object only."]
    for c in report["unsupported"]:
        lines.append(f'- In {c["where"]}: "{c["text"]}" is not in (and does not derive from) '
                     "the evidence for the findings cited there. Use a number from the data, "
                     "or remove it.")
    lines += [f"- {i}" for i in report["issues"]]
    return "\n".join(lines)


def _print_event(kind, **d):
    if kind == "draft":
        r = d["report"]
        bad = len(r["unsupported"]) + len(r["issues"])
        print(f"  draft {d['attempt']}: {r['checked']} numbers checked, "
              f"{'all supported ✓' if r['ok'] else f'{bad} problem(s) -> revising'}")


def write_commentary(llm, run: dict, max_revisions: int = 2, on_event=_print_event) -> dict:
    facts, raw, problems = _facts(run), None, None
    for attempt in range(max_revisions + 1):
        messages = [{"role": "system", "content": NARRATOR_PROMPT},
                    {"role": "user", "content": facts}]
        if raw is not None:                                    # revision round
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": problems}]
        raw = llm.chat(messages).content or ""
        try:
            commentary = _parse(raw)
            report = verify_commentary(commentary, run)
        except ValueError as e:                                # includes JSONDecodeError
            commentary = None
            report = {"ok": False, "checked": 0, "unsupported": [], "issues": [
                f"Reply was not valid JSON ({e}). Reply with ONLY the JSON object."]}
        on_event("draft", attempt=attempt + 1, report=report)
        if report["ok"]:
            break
        problems = _feedback(report)
    return {"commentary": commentary, "verification": report, "revisions": attempt}


if __name__ == "__main__":
    from llm import LLM
    src = Path("runs/latest.json")
    if not src.exists():
        raise SystemExit("Run `python investigator.py` first: it creates runs/latest.json.")
    run = json.loads(src.read_text())

    print("Checking the findings against their own evidence ...")
    for r in verify_findings(run):
        extra = "" if r["ok"] else f"  amount_ok={r['amount_ok']}, unsupported={[c['text'] for c in r['unsupported']]}"
        print(f"  {'✓' if r['ok'] else '✗'} {r['finding']}{extra}")

    print("\nWriting commentary ...")
    out = write_commentary(LLM(), run)
    Path("runs/commentary.json").write_text(json.dumps(out, indent=2, default=str))
    c, v = out["commentary"], out["verification"]
    if c:
        print(f"\n{c['headline']}\n")
        for p in c["paragraphs"]:
            print(f"[{', '.join(p.get('finding_ids', []))}] {p['text']}\n")
    status = "ALL NUMBERS TRACED TO EVIDENCE ✓" if v["ok"] else "UNRESOLVED PROBLEMS ✗"
    print(f"Verification: {status}  ({v['checked']} numbers checked, "
          f"{out['revisions']} revision(s))")
    for c_ in v["unsupported"]:
        print(f"  unsupported in {c_['where']}: {c_['text']}")
    for i in v["issues"]:
        print(f"  issue: {i}")
    print("\nSaved runs/commentary.json")