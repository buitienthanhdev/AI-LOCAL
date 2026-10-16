"""Export the assistant's verified successes as a training/evaluation dataset (JSONL).

    python scripts/export_training_data.py [--out logs/train/dataset.jsonl] [--min-steps 2]

One line per task that finished `completed` with `completion_basis == "verified"`:
  {"task_id", "goal", "steps": [{"tool", "arg_keys", "thought"}], "answer"}
Argument VALUES are left out (paths, names, secrets); only the argument names are kept. Nothing here trains anything: it
is the curated, human-reviewable input for an OPTIONAL later fine-tune (LoRA with rehearsal of earlier data) that must be
started by a person, never by a chat. See docs/continual-learning.md.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def rows(min_steps: int):
    for path in sorted((ROOT / "memory" / "tasks").glob("*.json")):
        if path.name.endswith((".request.json", ".response.json")) or path.name.startswith("_"):
            continue
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if task.get("status") != "completed" or task.get("completion_basis") != "verified":
            continue
        steps = [s for s in task.get("steps", []) if s.get("status") == "succeeded" and not str(s.get("tool", "")).startswith("task.")]
        if len(steps) < min_steps:
            continue
        yield {
            "task_id": task["task_id"],
            "goal": task["goal"],
            "steps": [{"tool": s["tool"], "arg_keys": sorted((s.get("args") or {}).keys()), "thought": (s.get("thought") or "")[:200]} for s in steps],
            "answer": (task.get("final_summary") or "")[:1200],
        }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "logs" / "train" / f"dataset-{datetime.now():%Y%m%d}.jsonl"))
    ap.add_argument("--min-steps", type=int, default=2)
    a = ap.parse_args()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open("w", encoding="utf-8") as f:
        for row in rows(a.min_steps):
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    print(f"{n} verified successes -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
