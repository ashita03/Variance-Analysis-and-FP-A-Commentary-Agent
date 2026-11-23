# Variance Analysis & FP&A Commentary Agent

A multi-agent system that investigates why a company missed its budget, writes the management
commentary, and **proves every number in that commentary traces back to a database query**.

## The problem

Every month, FP&A analysts compare budget to actuals, dig into the big gaps, and write
commentary for leadership. The digging is slow, and the obvious shortcut, asking an LLM to write
the commentary, has a fatal flaw: language models state wrong numbers with total confidence.

## The idea

Use the LLM only where judgment is needed, and use plain code for everything that can be
checked mechanically.

| Step | Done by | Why |
|---|---|---|
| Which variances are material (>= 5% and >= $10K)? | Plain code | Arithmetic |
| Price vs. volume split | Plain code (a tool) | The algebra is exact, so the model shouldn't re-derive it |
| *Why* did it happen? | **Investigator agent** (LLM + tools) | Open-ended drill-down |
| Write the commentary | **Narrator agent** (LLM) | Writing |
| Is every number real? | **Verifier** (plain code) | A checker that can't be talked into agreeing |

## Architecture

```
generate_data.py ──► data/finance.duckdb  (24 months, planted ground truth)
                           │
 variance.py ◄── tools.py ◄┘     read-only, validated SQL; every result gets a citable ID
                    │
          investigator.py (+ llm.py)        tool loop, step cap, forced wrap-up
                    │  findings, each citing query IDs as evidence
                    ▼
           narrator.py ◄──────┐             writes commentary as tagged JSON paragraphs
                    │         │ feedback: "$26,315 in paragraph 2 isn't in the evidence"
                    ▼         │
           verifier.py ───────┘             deterministic; max 2 revisions
                    │
          evaluate.py   scores everything against an oracle computed from the data
                    │
       api.py (FastAPI) ──► static/index.html   dashboard, summary, evidence drill-down
```

### Design decisions worth knowing

- **Tools match the analyst's questions.** The first version made the model hand-write
  price/volume SQL and it burned most of its step budget. A `revenue_price_volume` tool that
  returns the split (and reconciles to the variance) fixed that.
- **Safety is layered.** Model-written SQL is untrusted: the connection is read-only with
  external file access disabled, a validator allows one SELECT on known tables, and results are
  row-capped. Tool errors are returned to the model as feedback, so it can self-correct.
- **Enforce in code, don't request in prose.** A "2 steps left" nudge was ignored by the model.
  When the step budget runs out, the harness now makes one more call with `tool_choice="none"`
  and asks for a summary, so a run can't end silently.
- **Evidence is scoped.** A paragraph tagged `f1` may only use numbers from the evidence
  that finding cited. A correct number attached to the wrong story is flagged.
- **Sign convention is defined.** `amount_usd` is impact on **profit**: negative is
  unfavorable, whether it's a revenue shortfall or a cost overrun.

## How the verifier works

It extracts every number from the commentary and checks it against the evidence, at the
precision the writer used (`$85K` matches 84,986.45). Allowed derivations are deliberately
narrow, the kind a human auditor would accept:

- any value in the cited evidence, or a column total
- the difference or percent change of a **budget column and its matching actual column**
  (`units_budget`/`units_actual`, or abbreviated `pb`/`pa`)
- a finding's own amount, or the sum of the cited findings

It checks **numbers, not causal claims**, and ignores years and single-digit counts.

### A failure that shaped the design

The first verifier allowed a derived number from *any* pair of columns in a row. A built-in
stress test (corrupt each number in the commentary by ~3%, count how many the verifier catches)
found it **missed 2 of 14 injected errors**. The cause: on dense evidence, meaningless
derivations such as a percent change between a price and a unit count matched wrong numbers by
coincidence. Restricting derivations to budget/actual pairs fixed it: **14/14 caught**, with a
regression test built on dense evidence so it stays fixed.

## Evaluation

Planted ground truth on synthetic data: three hidden drivers (a West volume shortfall, a Summit
Jacket price cut, a one-time $85K vendor charge) plus one decoy (a +40% line item worth only
$3.2K, which must *not* be reported). The oracle recomputes true dollar impacts straight from the
database. It does not trust the agent.

Checks per run: each driver found with the **right type and amount** (within 1% or $100), decoy
ignored, findings traced to their evidence, commentary traced to evidence, and **reconciliation**
(do findings explain the total variance within 20%?). The eval also warns when a summary claims
"reconciled" while a gap remains.

**Single saved run:** 7/7 checks passed; verifier stress test caught 14/14. Reconciliation
residuals were 10% (revenue; the offsetting volume lift on Summit Jacket is not in any finding)
and 3% (expenses).

**Repeated live runs** (`python evaluate.py --live N`):

To be updated

## Quickstart

Requires Python 3.10+.

```bash
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env                                   # then add your API key
python generate_data.py
pytest                                                 # 51 tests, no API key needed
```

```bash
python investigator.py        # agent 1  -> runs/latest.json
python narrator.py            # agent 2  -> runs/commentary.json
python evaluate.py            # score the saved run + stress-test the verifier (free)
python evaluate.py --live 3   # run the whole pipeline 3x and report pass rates
uvicorn api:app --reload      # web app at http://localhost:8000
```

### LLM configuration

Any OpenAI-compatible provider works. Edit `.env`:

```
LLM_API_KEY=...
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/   # Gemini example
LLM_MODEL=<a Flash-tier model id from your provider>
LLM_REASONING_EFFORT=low        # optional, for "thinking" models
MAX_AGENT_STEPS=15
```

The client retries rate limits and server errors (429, 5xx) with growing waits, and fails fast
on mistakes like a wrong model name or bad key. Free tiers change often, so check your
provider's current limits.

## Project structure

```
variance-agent/
├── generate_data.py    synthetic data, planted drivers, answer key
├── variance.py         deterministic material-variance detector
├── llm.py              provider-agnostic client with selective retries
├── tools.py            agent tools, SQL safety layer, citable query log
├── investigator.py     agent 1: tool loop, step cap, forced wrap-up
├── narrator.py         agent 2: generate -> verify -> revise loop
├── verifier.py         deterministic number checker (no LLM)
├── evaluate.py         oracle scoring, reconciliation, stress test, --live N
├── api.py              FastAPI backend
├── static/index.html   dashboard, summary and evidence drill-down (single file)
├── tests/              51 tests, including scripted-model tests of the agent loops
├── runs/               saved demo run (commit these)
└── Dockerfile
```

## Testing without an LLM

Agent loops are tested with a **scripted fake model** that replays fixed replies. That makes the
harness (tool dispatch, error recovery, step cap, wrap-up, revise loop) deterministic and free
to test. The model's *judgment* is what the eval measures.

## Deploy

The container regenerates the data at build time and serves the saved run in `runs/`, so
visitors cost nothing and the demo needs no API key.

```bash
docker build -t variance-agent . && docker run -p 8000:8000 variance-agent
```

On a host like Render: create a Web Service from the repo using the Dockerfile and set the
health check to `/api/health`. Free tiers typically sleep when idle, so the first load can be
slow.

## Limitations

- **Synthetic data.** The company is fictional, with three planted drivers. Real ledgers are
  messier, so these results show the architecture works, not that it's production-ready.
- **The verifier checks numbers, not reasoning.** A wrong causal claim with correct numbers
  would pass, and qualitative wording ("unbudgeted") isn't checked.
- **Small whole numbers can still match a real value by coincidence.** The stress test measures
  this. Having the narrator cite a specific query and column per number would remove it.
- **Results vary by model.** Free models can skip steps (for example, not noting when a change
  started), which is why the eval reports repeated runs.
- **No live-run endpoint yet.** The public demo serves a saved run.

## Possible next steps

- Live-run button with streamed steps, a daily cap, and rate limiting
- Per-number source citations (`query.column`) instead of pooled evidence
- Multi-month trend analysis and more driver types (mix, timing)
- Real data connectors (a general ledger export)