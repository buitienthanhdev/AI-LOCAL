"""Hint optimiser: the bench decides which "learned hints" the planner keeps (a small GEPA/DSPy-style loop, no dependency).

    python tests/bench/optimize_hints.py                      # print the candidate hints built from the failure memory
    python tests/bench/optimize_hints.py --evaluate --only f-xlsx-average,m-report --rounds 1

Candidates come from `memory/persistent.db` (`failures` rows that have a recovery and were seen at least twice): one short
imperative line each. With --evaluate the chosen bench tasks run without hints, then with them (the planner reads
`memory/optimizer/hints.md` on every task); the hints are kept only if the pass rate did not drop, otherwise the file is
removed again. A run costs as many bench runs as `--rounds` x 2, so it is meant for idle nights, not for the chat.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HINTS = ROOT / "memory" / "optimizer" / "hints.md"
MAX_HINTS = 8


def candidates() -> list[str]:
    db = ROOT / "memory" / "persistent.db"
    if not db.exists():
        return []
    con = sqlite3.connect(db)
    out: list[str] = []
    for tool, code, context, recovery, count in con.execute(
        "SELECT tool, error_code, context, recovery, count FROM failures WHERE disabled = 0 AND recovery IS NOT NULL AND count >= 2 ORDER BY count DESC LIMIT 40"
    ):
        recovery = re.sub(r"\s+", " ", recovery).strip()
        if len(recovery) < 8 or re.match(r"succeeded at s\d+", recovery):   # "worked afterwards" says nothing about how
            continue
        out.append(f"When {tool} fails with {code}: {recovery[:160]}")
    seen, unique = set(), []
    for line in out:
        if line not in seen:
            seen.add(line)
            unique.append(line)
    return unique[:MAX_HINTS]


def run_bench(only: str) -> float:
    proc = subprocess.run([sys.executable, str(ROOT / "tests" / "bench" / "run_bench.py"), "--profile", "product", "--only", only], capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    passed = len(re.findall(r"\bPASS\b", proc.stdout))
    failed = len(re.findall(r"\b(FAIL|FALSE SUCCESS)\b", proc.stdout))
    return passed / max(1, passed + failed)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evaluate", action="store_true")
    ap.add_argument("--only", default="f-xlsx-average,m-report,m-biggest-csv")
    ap.add_argument("--rounds", type=int, default=1)
    a = ap.parse_args()
    hints = candidates()
    print(json.dumps({"candidates": hints}, ensure_ascii=False, indent=2))
    if not a.evaluate or not hints:
        return 0
    HINTS.parent.mkdir(parents=True, exist_ok=True)
    base = after = 0.0
    for _ in range(a.rounds):
        HINTS.unlink(missing_ok=True)
        base += run_bench(a.only)
        HINTS.write_text("\n".join(f"- {h}" for h in hints) + "\n", encoding="utf-8")
        after += run_bench(a.only)
    base, after = base / a.rounds, after / a.rounds
    print(f"pass rate without hints {base:.0%}, with hints {after:.0%}")
    if after < base:
        HINTS.unlink(missing_ok=True)
        print("hints removed: they did not help")
    else:
        print(f"hints kept in {HINTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
