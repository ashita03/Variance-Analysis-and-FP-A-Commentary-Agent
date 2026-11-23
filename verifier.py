"""Deterministic verifier: every number in the commentary must trace back to cited evidence.

No LLM involved. The writer can be wrong; the checker cannot be talked into agreeing.

A number is SUPPORTED if, at the precision the writer used, it matches one of:
  - a number in the evidence queries the cited findings point to (direct),
  - the difference or percent change of a budget column and its matching actual column,
    or a column total (derived),
  - a finding's own amount, or the sum of cited findings' amounts.
Limits: this checks NUMBERS, not causal claims ("because of X"), and it ignores years and
bare single-digit counts like "3 drivers".
"""
import bisect
import math
import re
from decimal import ROUND_HALF_UP, Decimal

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


_ACT, _BUD = {"actual", "act"}, {"budget", "bud", "plan"}


def _stem(name):
    """Which side of a budget/actual pair is this column? ('units','a') for units_actual or ua."""
    n = str(name).lower()
    toks = [t for t in re.split(r"[^a-z0-9]+", n) if t]
    for words, kind in ((_ACT, "a"), (_BUD, "b")):
        if any(t in words for t in toks):
            return tuple(t for t in toks if t not in words), kind
    m = re.fullmatch(r"([a-z]+)([ab])", n)            # abbreviations: pa/pb, ua/ub, ra/rb
    return ((m.group(1),), m.group(2)) if m else None


def _pairs(names):
    """Index pairs (actual_col, budget_col) that share a stem. The ONLY derivable pairs."""
    act, bud = {}, {}
    for i, n in enumerate(names):
        if st := _stem(n):
            (act if st[1] == "a" else bud)[st[0]] = i
    return [(act[k], bud[k]) for k in act if k in bud]


def _pct_named(name) -> bool:
    return bool(re.search(r"pct|percent|ratio|margin|rate|%", str(name).lower()))


def build_pools(queries: dict, query_ids, extra_amounts=()):
    """Sorted candidate pools: (money-like numbers, percentages), as absolute values.

    Deliberately NARROW: arbitrary column pairs would let nonsense (price vs unit count)
    'support' a wrong number by coincidence. Allowed:
      money: any cell; actual-budget difference of a matching pair; column totals; finding sums
      pct:   cells in percent-named columns; percent change of a matching pair
    """
    money, pct = set(), set()
    r = lambda x: round(abs(x), 6)
    for qid in query_ids:
        q = queries.get(qid) or {}
        rows, cols = q.get("rows"), q.get("columns")
        if not isinstance(rows, list):
            continue
        totals: dict = {}
        for row in rows:
            if isinstance(row, dict):
                names, vals = list(row.keys()), list(row.values())
            elif isinstance(row, (list, tuple)):
                vals = list(row)
                names = list(cols) if cols and len(cols) == len(vals) else [None] * len(vals)
            else:
                continue
            for i, v in enumerate(vals):
                if _isnum(v):
                    money.add(r(v))
                    totals[i] = totals.get(i, 0) + v
                    if _pct_named(names[i]):
                        pct.add(r(v))
            for ia, ib in _pairs(names):
                a, b = vals[ia], vals[ib]
                if _isnum(a) and _isnum(b):
                    money.add(r(a - b))
                    if b:
                        pct.add(r((a - b) / b * 100))
        money.update(r(t) for t in totals.values())
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