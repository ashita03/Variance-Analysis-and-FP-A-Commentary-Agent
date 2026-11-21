# Variance Analysis Agent
 
A multi-agent system that investigates budget-vs-actual variances on a synthetic
company's books, writes management commentary, and verifies every number in it.
 
## Setup
 
Requires Python 3.10+.
 
**macOS / Linux**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then add your API key
```
 
**Windows (PowerShell)**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # then add your API key
```
 
Leave the environment with `deactivate`. Re-activate it each new terminal session.
 
## Run
 
```bash
python generate_data.py     # builds data/finance.duckdb + data/ground_truth.json
python variance.py          # deterministic variance detection (no LLM)
pytest                      # tests (added as we build)
```
 
## Structure
 
| File | Purpose |
|---|---|
| `generate_data.py` | Synthetic data with planted drivers + answer key |
| `variance.py` | Deterministic material-variance detector |
| `.env` | Your API key and model settings (git-ignored) |
 
## Roadmap
 
1. [x] Data layer + variance detector
2. [ ] Investigator agent (tool loop, safe SQL tool, findings log)
3. [ ] Narrative agent + verifier
4. [ ] FastAPI + UI
5. [ ] Eval script (planted drivers found? injected errors caught?)
6. [ ] Docker + deploy
 