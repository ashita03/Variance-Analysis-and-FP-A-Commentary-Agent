import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def _data():
    os.chdir(ROOT)
    if not (ROOT / "data" / "finance.duckdb").exists():
        subprocess.run([sys.executable, "generate_data.py"], cwd=ROOT, check=True)