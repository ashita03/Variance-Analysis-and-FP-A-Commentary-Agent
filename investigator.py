"""Investigator agent: a tool-using loop that works out WHY variances happened."""
import json
import os
from pathlib import Path

from tools import TOOLS, Workspace

SYSTEM_PROMPT = """You are a senior FP&A analyst investigating budget-vs-actual variances.

DATA (DuckDB, read-only), one row per month:
- revenue(month, region, product, units_budget, units_actual, price_budget, price_actual, revenue_budget, revenue_actual)
- expenses(month, department, category, vendor, budget, actual)
`month` is a DATE (first of month), e.g. '2026-10-01'. Regions: East, West, Central, South.

METHOD
1. Call get_material_variances for the target month. Only these variances are worth explaining.
2. Drill down with run_query to find the ROOT CAUSE of each. Variances at different levels often
   share one cause (a vendor charge shows up in the vendor, category and department rows).
   Report each distinct root cause ONCE, at the most specific level.
3. For revenue, use revenue_price_volume (do NOT hand-write price/volume SQL). Run it by region and
   by product to see where price or volume effects concentrate, and for a prior month to see when
   a change started, and say in your finding when it began. Use run_query for anything else,
   e.g. expense drill-downs.
4. For each root cause call record_finding with its dollar impact and the query ids that prove it.
   amount_usd is the impact on PROFIT: negative = unfavorable (revenue shortfall or cost overrun),
   positive = favorable. Never state a number you did not get from a query result.
5. Never describe the variances as 'reconciled' or 'fully explained'; a separate check measures that.
   Do not report anything below the materiality thresholds. When every material variance is
   explained, reply with a short plain-text summary and NO tool call.

You have a limited number of steps, so be efficient."""


def _tool_call_dict(c) -> dict:
    """Rebuild a tool call for the conversation history.

    Real SDK objects are dumped whole, so provider-specific extras survive the round trip.
    Gemini 3 models attach a 'thought signature' to tool calls that must be sent back.
    """
    if hasattr(c, "model_dump"):
        d = c.model_dump(exclude_none=True)
        d["type"] = "function"
        return d
    return {"id": c.id, "type": "function",
            "function": {"name": c.function.name, "arguments": c.function.arguments}}


def _assistant_dict(msg) -> dict:
    d = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        d["tool_calls"] = [_tool_call_dict(c) for c in msg.tool_calls]
    return d


def _print_event(kind, **data):
    if kind == "tool_call":
        print(f"  step {data['step']}: {data['name']}({json.dumps(data['args'])[:110]})")
    elif kind == "tool_error":
        print(f"           ↳ error fed back to model: {data['error'][:100]}")
    elif kind == "wrapup":
        print("  step budget used: asking for a final summary (tools disabled)")
    elif kind == "nudge":
        print("           ↳ 2 steps left: nudging the agent to wrap up")
    elif kind == "finding":
        print(f"           ↳ FINDING {data['id']}: {data['summary'][:110]}")


def investigate(llm, month: str = "2026-10-01", max_steps: int | None = None,
                on_event=_print_event, db_path: str | None = None) -> dict:
    max_steps = max_steps or int(os.environ.get("MAX_AGENT_STEPS", 12))
    ws = Workspace(db_path) if db_path else Workspace()
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Investigate the material variances for {month}."}]
    final_text, steps, finished = "", 0, False

    while steps < max_steps:                                   # <- the step cap
        if steps > 0 and max_steps - steps == 2:               # nudge before the cap bites
            messages.append({"role": "user", "content": "You have 2 steps left. Record any remaining "
                             "findings now, then reply with a short final summary and no tool call."})
            on_event("nudge")
        steps += 1
        msg = llm.chat(messages, tools=TOOLS)                  # 1. model decides
        messages.append(_assistant_dict(msg))
        if not msg.tool_calls:                                 # 2. no tool call = it's done
            final_text, finished = msg.content or "", True
            break
        for call in msg.tool_calls:                            # 3. WE execute the tools
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = None
            on_event("tool_call", step=steps, name=call.function.name, args=args)
            out = ({"error": "Arguments were not valid JSON."} if args is None
                   else ws.call(call.function.name, args))
            if "error" in out:
                on_event("tool_error", error=out["error"])
            elif "recorded" in out:
                on_event("finding", **ws.findings[-1])
            messages.append({"role": "tool", "tool_call_id": call.id,   # 4. result goes back
                             "content": json.dumps(out, default=str)})

    wrapped_up = False
    if not finished:                                           # budget spent: force a summary
        on_event("wrapup")
        messages.append({"role": "user", "content": "Your step budget is used up. Do not call any "
                         "more tools. Write a short final summary of the findings recorded so far, "
                         "and note anything left unexplained."})
        msg = llm.chat(messages, tools=TOOLS, tool_choice="none")
        final_text = msg.content or ""                         # any tool calls here are ignored
        messages.append({"role": "assistant", "content": final_text})
        wrapped_up = True

    return {"month": month, "findings": ws.findings, "queries": ws.results,
            "summary": final_text, "steps": steps, "hit_step_cap": not finished,
            "wrapped_up": wrapped_up, "transcript": messages}


def check_against_truth(findings, path="data/ground_truth.json") -> None:
    """Rough keyword check of the planted answer key (the real eval comes later)."""
    truth = json.loads(Path(path).read_text())
    text = " ".join(f["summary"] for f in findings).lower()
    print("\nPlanted drivers:")
    for d in truth["drivers"]:
        key = str(list(d["where"].values())[-1]).lower()
        print(f"  {'✓' if key in text else '✗'} {d['id']}")
    decoy_hit = "travel" in text and "engineering" in text
    print(f"  {'✗ reported the decoy!' if decoy_hit else '✓ ignored the decoy'}")


if __name__ == "__main__":
    from llm import LLM
    print("Investigating 2026-10 ...\n")
    result = investigate(LLM())
    Path("runs").mkdir(exist_ok=True)
    Path("runs/latest.json").write_text(json.dumps(result, indent=2, default=str))
    print(f"\nDone in {result['steps']} steps"
          f"{' (HIT STEP CAP)' if result['hit_step_cap'] else ''}. "
          f"{len(result['findings'])} findings:\n")
    for f in result["findings"]:
        print(f"  [{f['id']}] {f['driver_type']:<9} ${f['amount_usd']:>10,.0f}  {f['summary']}")
    if result["summary"]:
        print(f"\nSummary:\n  {result['summary']}")
    check_against_truth(result["findings"])
    print("\nSaved runs/latest.json")