"""Deterministic verifier: every number in the commentary must trace back to cited evidence.

No LLM involved. The writer can be wrong; the checker cannot be talked into agreeing.

A number is SUPPORTED if, at the precision the writer used, it matches one of:
  - a number in the evidence queries the cited findings point to (direct),
  - a within-row difference or percent change, or a column total (derived),
  - a finding's own amount, or the sum of cited findings' amounts.
Limits: this checks NUMBERS, not causal claims ("because of X"), and it ignores years and
bare single-digit counts like "3 drivers".
"""
import bisect
import math
import re
from decimal import ROUND_HALF_UP, Decimal
from itertools import permutations

_ISO_DATE = re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?\b")
_NUM = re.compile(r"(?<![\w.])(?P<cur>\$)?(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?P<frac>\.\d+)?"
                  r"(?P<suf>%|[KkMm](?![A-Za-z]))?")


def extract_numbers(text: str) -> list[dict]:
    clean = _ISO_DATE.sub(lambda m: " " * len(m.group()), text)
    out = []
    for m in _NUM.finditer(clean):
        frac, suf = m.group("frac") or "", m.group("suf") or ""
        value = Decimal(m.group("int").replace(",", "") + frac)
        plain = not (m.group("cur") or suf or frac or "," in m.group("int"))
        if plain and (2000 <= value <= 2099 or value < 10):   # a year, or a count like "3"
            continue
        out.append({"text": m.group(0), "value": value,
                    "decimals": len(frac) - 1 if frac else 0,
                    "scale": {"k": 1_000, "m": 1_000_000}.get(suf.lower(), 1),
                    "is_pct": suf == "%", "span": m.span()})
    return out


def _isnum(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def build_pools(queries: dict, query_ids, extra_amounts=()):
    """Sorted candidate pools: (money-like numbers, percentages). All stored as absolute values."""
    money, pct = set(), set()
    r = lambda x: round(abs(x), 6)
    for qid in query_ids:
        rows = (queries.get(qid) or {}).get("rows")
        if not isinstance(rows, list):
            continue
        col_sums: dict = {}
        for row in rows:
            if isinstance(row, dict):
                items = [(k, v) for k, v in row.items() if _isnum(v)]
            elif isinstance(row, (list, tuple)):
                items = [(i, v) for i, v in enumerate(row) if _isnum(v)]
            else:
                continue
            for k, v in items:
                money.add(r(v)); pct.add(r(v))
                col_sums[k] = col_sums.get(k, 0) + v
            for a, b in permutations([v for _, v in items], 2):
                money.add(r(a - b))                        # within-row difference
                if b:
                    pct.add(r((a - b) / b * 100))          # within-row percent change
        for s in col_sums.values():                        # column totals
            money.add(r(s)); pct.add(r(s))
    for a in extra_amounts:
        money.add(r(a))
    return sorted(money), sorted(pct)


def _match(claim: dict, pool: list[float]) -> bool:
    d, v, scale = claim["decimals"], claim["value"], claim["scale"]
    half = Decimal(5) * Decimal(10) ** (-d - 1)            # half a unit in the last place
    lo, hi = float((v - half) * scale) - 1e-6, float((v + half) * scale) + 1e-6
    q = Decimal(1).scaleb(-d)
    i = bisect.bisect_left(pool, lo)
    while i < len(pool) and pool[i] <= hi:
        if Decimal(repr(pool[i] / scale)).quantize(q, rounding=ROUND_HALF_UP) == v:
            return True
        i += 1
    return False


def verify_text(text: str, money, pct, where: str) -> list[dict]:
    return [{"where": where, "text": c["text"],
             "supported": _match(c, pct if c["is_pct"] else money)}
            for c in extract_numbers(text)]


def _pools_for(run: dict, ids):
    findings = {f["id"]: f for f in run["findings"]}
    chosen = [findings[i] for i in ids if i in findings] or list(findings.values())
    evidence = {q for f in chosen for q in f.get("evidence_query_ids", [])}
    amounts = [f["amount_usd"] for f in chosen]
    return build_pools(run["queries"], evidence,
                       amounts + [sum(amounts), sum(abs(a) for a in amounts)])


def verify_findings(run: dict) -> list[dict]:
    """Check each finding's amount and summary against ITS OWN evidence (not its own amount)."""
    results = []
    for f in run["findings"]:
        money, pct = build_pools(run["queries"], f.get("evidence_query_ids", []))
        val = Decimal(str(round(abs(f["amount_usd"]), 2)))
        amount_ok = _match({"value": val, "decimals": max(0, -val.as_tuple().exponent),
                            "scale": 1}, money)
        bad = [c for c in verify_text(f["summary"], money, pct, f["id"]) if not c["supported"]]
        results.append({"finding": f["id"], "amount_ok": amount_ok, "unsupported": bad,
                        "ok": amount_ok and not bad})
    return results


def verify_commentary(commentary, run: dict) -> dict:
    if (not isinstance(commentary, dict) or not isinstance(commentary.get("headline"), str)
            or not isinstance(commentary.get("paragraphs"), list) or not commentary["paragraphs"]):
        return {"ok": False, "checked": 0, "unsupported": [], "issues": [
            "Commentary must be an object with a 'headline' string and a non-empty "
            "'paragraphs' list."]}
    known = {f["id"] for f in run["findings"]}
    issues, covered = [], set()
    sections = [("headline", commentary["headline"], sorted(known))]
    for i, p in enumerate(commentary["paragraphs"], 1):
        if not isinstance(p, dict) or not isinstance(p.get("text"), str):
            issues.append(f"Paragraph {i} must be an object with 'text' and 'finding_ids'.")
            continue
        ids = p.get("finding_ids") or []
        if bad := [x for x in ids if x not in known]:
            issues.append(f"Paragraph {i} cites unknown finding ids {bad}.")
        covered |= set(ids) & known
        sections.append((f"paragraph {i}", p["text"], [x for x in ids if x in known]))
    issues += [f"Finding {m} is not covered by any paragraph." for m in sorted(known - covered)]
    claims = []
    for where, text, ids in sections:
        money, pct = _pools_for(run, ids)
        claims += verify_text(text, money, pct, where)
    unsupported = [c for c in claims if not c["supported"]]
    return {"ok": not issues and not unsupported, "checked": len(claims),
            "unsupported": unsupported, "issues": issues}