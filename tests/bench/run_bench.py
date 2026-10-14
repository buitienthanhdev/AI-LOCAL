"""End-to-end benchmark of the agent: runs real requests through `agent run` and judges the results itself.

The agent's own claim ("completed") is never taken as success: every task has checks the runner evaluates on
real state (files, workbooks, numbers in the answer compared with the machine's actual values, tools used).
A task that the agent reports as completed while a check fails is counted as a FALSE SUCCESS, the worst
outcome.

    python tests/bench/run_bench.py --profile baseline            # all tasks, local brain only
    python tests/bench/run_bench.py --profile google --only file-create,excel-report --repeat 3
    python tests/bench/run_bench.py --compare logs/bench/A.json logs/bench/B.json

Profiles pin the brain to the local Ollama model (LGA_BRAIN=local) and set its thinking mode and
sampling through request metadata, so they are compared on the same model with nothing else changed.
Reports: logs/bench/<timestamp>-<profile>.json and .md.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "target" / "release" / "agent.exe"
TASK_FILES = sorted(Path(__file__).parent.glob("tasks*.json"))
WORK = Path(r"C:\temp\lga-bench")
REPORTS = ROOT / "logs" / "bench"
TASK_TIMEOUT_S = 900
WEEKDAYS_VI = ["thứ hai", "thứ ba", "thứ tư", "thứ năm", "thứ sáu", "thứ bảy", "chủ nhật"]

PROFILES: dict[str, dict[str, str]] = {
    # The server's own defaults (temperature 0.8, top_k 40, top_p 0.95), thinking off.
    "baseline": {"LGA_BRAIN": "local", "LGA_THINKING": "off"},
    # Google's recommended Gemma 4 sampling.
    "google": {"LGA_BRAIN": "local", "LGA_THINKING": "off", "LGA_TEMPERATURE": "1.0", "LGA_TOP_P": "0.95", "LGA_TOP_K": "64"},
    "google-think": {"LGA_BRAIN": "local", "LGA_THINKING": "on", "LGA_TEMPERATURE": "1.0", "LGA_TOP_P": "0.95", "LGA_TOP_K": "64"},
    # Low temperature: near-deterministic tool calls.
    "precise": {"LGA_BRAIN": "local", "LGA_THINKING": "off", "LGA_TEMPERATURE": "0.2", "LGA_TOP_P": "0.95", "LGA_TOP_K": "40"},
    "precise-think": {"LGA_BRAIN": "local", "LGA_THINKING": "on", "LGA_TEMPERATURE": "0.2", "LGA_TOP_P": "0.95", "LGA_TOP_K": "40"},
    # Same, with the model's native tool calling instead of the schema-constrained JSON reply.
    "native-think": {"LGA_BRAIN": "local", "LGA_THINKING": "on", "LGA_TEMPERATURE": "1.0", "LGA_TOP_P": "0.95", "LGA_TOP_K": "64",
                     "LGA_NATIVE_TOOLS": "on"},
    "json-think": {"LGA_BRAIN": "local", "LGA_THINKING": "on", "LGA_TEMPERATURE": "1.0", "LGA_TOP_P": "0.95", "LGA_TOP_K": "64",
                   "LGA_NATIVE_TOOLS": "off"},
    # Ollama cloud models (through the local Ollama daemon; free plan models).
    "cloud-gptoss": {"LGA_BRAIN": "cloud", "LGA_MODEL": "gpt-oss:120b-cloud", "LGA_THINKING": "on"},
    "cloud-nemotron": {"LGA_BRAIN": "cloud", "LGA_MODEL": "nemotron-3-ultra:cloud", "LGA_THINKING": "on"},
    "cloud-gemma31": {"LGA_BRAIN": "cloud", "LGA_MODEL": "gemma4:31b-cloud", "LGA_THINKING": "on"},
    "cloud-gptoss20": {"LGA_BRAIN": "cloud", "LGA_MODEL": "gpt-oss:20b-cloud", "LGA_THINKING": "on"},
    "cloud-nemotron-super": {"LGA_BRAIN": "cloud", "LGA_MODEL": "nemotron-3-super:cloud", "LGA_THINKING": "on"},
    # A model pulled into the local Ollama (through the same Ollama path as the cloud models).
    "ollama-qwen38": {"LGA_BRAIN": "cloud", "LGA_MODEL": "qwen3.8:27b", "LGA_THINKING": "off"},
    "ollama-qwen38-think": {"LGA_BRAIN": "cloud", "LGA_MODEL": "qwen3.8:27b", "LGA_THINKING": "on"},
    "cloud-nemotron-nano": {"LGA_BRAIN": "cloud", "LGA_MODEL": "nemotron-3-nano:30b-cloud", "LGA_THINKING": "on"},
    # Whatever the gateway's configured brain order picks (local first, cloud fallbacks): the product as shipped.
    "product": {},
}


# ----------------------------------------------------------------------------------------------- setup

def expand(value, folder: Path):
    if isinstance(value, str):
        return value.replace("{dir}", str(folder))
    if isinstance(value, list):
        return [expand(v, folder) for v in value]
    if isinstance(value, dict):
        return {expand(k, folder): expand(v, folder) for k, v in value.items()}
    return value


def prepare(task: dict, folder: Path) -> None:
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    setup = expand(task.get("setup", {}), folder)
    for path, content in setup.get("files", {}).items():
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")
    for path, size in setup.get("sized_files", {}).items():
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"\0" * int(size))
    for path in setup.get("dirs", []):
        Path(path).mkdir(parents=True, exist_ok=True)
    if z := setup.get("zip"):
        import zipfile
        with zipfile.ZipFile(z["path"], "w") as zf:
            for name, content in z["files"].items():
                zf.writestr(name, content)
    if x := setup.get("xlsx"):
        import openpyxl
        wb = openpyxl.Workbook()
        for row in x["rows"]:
            wb.active.append(row)
        wb.save(x["path"])
    if dx := setup.get("docx"):
        import docx
        doc = docx.Document()
        for para in dx["paragraphs"]:
            doc.add_paragraph(para)
        doc.save(dx["path"])
    now = time.time()
    for path, offset in setup.get("mtimes", {}).items():
        os.utime(path, (now + offset, now + offset))
    if note := setup.get("memory_note"):
        with sqlite3.connect(ROOT / "memory" / "persistent.db") as conn:
            conn.execute("INSERT OR IGNORE INTO user_notes (note, created_at) VALUES (?, ?)", (note, dt.datetime.now(dt.timezone.utc).isoformat()))
    if img := setup.get("image"):  # a simple flat picture: {"path", "size": [w, h], "background", "shapes": [...]}
        from PIL import Image, ImageDraw
        pic = Image.new("RGB", tuple(img.get("size", [480, 360])), img.get("background", "white"))
        draw = ImageDraw.Draw(pic)
        for shape in img.get("shapes", []):
            getattr(draw, shape["kind"])(shape["box"], fill=shape["fill"])
        pic.save(img["path"])
    if rows := setup.get("csv_rows"):
        with open(rows["path"], "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["ma", "ten", "so_luong"])
            for i in range(1, int(rows["rows"]) + 1):
                w.writerow([f"SP{i:03d}", f"Mục {i}", i * 3])


def cleanup(task: dict) -> None:
    for name in task.get("cleanup", {}).get("kill", []):  # windows the task leaves open on purpose (Paint)
        subprocess.run(["taskkill", "/F", "/IM", f"{name}.exe"], capture_output=True)
    if forget := task.get("cleanup", {}).get("forget"):
        db = ROOT / "memory" / "persistent.db"
        with sqlite3.connect(db) as conn:
            conn.execute("DELETE FROM user_notes WHERE note LIKE ?", (f"%{forget}%",))


# ----------------------------------------------------------------------------------------------- checks

def numbers(text: str) -> list[float]:
    out = []
    for m in re.finditer(r"\d+(?:[.,]\d+)?", text):
        try:
            out.append(float(m.group().replace(",", ".")))
        except ValueError:
            pass
    return out


def check(c: dict, run: dict) -> tuple[bool, str]:
    kind = c["kind"]
    summary = run.get("summary") or ""
    low = summary.lower()
    tools = [s["tool"] for s in run.get("steps", []) if s.get("status") == "succeeded"]
    if kind == "status":
        return run["status"] == c["is"], f"status {run['status']}"
    if kind == "summary_contains":
        return any(a.lower() in low for a in c["any"]), f"answer mentions one of {c['any']}"
    if kind == "summary_regex":
        return re.search(c["pattern"], summary) is not None, f"answer matches {c['pattern']}"
    if kind == "summary_not_regex":
        return re.search(c["pattern"], summary) is None, f"answer does not match {c['pattern']}"
    if kind == "summary_not_contains":
        return not any(a.lower() in low for a in c["any"]), f"answer does not mention {c['any']}"
    if kind == "summary_cmd":
        expected = subprocess.run(["powershell", "-NoProfile", "-Command", c["ps"]], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace").stdout.strip()
        if not expected:
            return False, "expected value unavailable"
        found = re.search(rf"\b{re.escape(expected)}\b", summary, re.I) if c.get("word") else expected.lower() in low
        return bool(found), f"answer contains the real value {expected!r}"
    if kind == "no_step_succeeded":
        return not any(t.startswith(c["prefix"]) for t in tools), f"no successful {c['prefix']} step"
    if kind == "memory_missing":
        with sqlite3.connect(ROOT / "memory" / "persistent.db") as conn:
            n = conn.execute("SELECT COUNT(*) FROM user_notes WHERE note LIKE ?", (f"%{c['text']}%",)).fetchone()[0]
        return n == 0, f"no remembered note contains {c['text']}"
    if kind == "no_window":
        # Applications close asynchronously (Word keeps its window title for a moment after WM_CLOSE): give
        # them a few seconds before calling a window "left open".
        deadline = time.monotonic() + 6.0
        while True:
            # Optional "process" narrows the check to that executable: a user's editor showing BAO_CAO_DOMAIN.md
            # must not count as a Word window titled bao_cao.docx.
            proc = f" -and $_.ProcessName -like '{c['process']}*'" if c.get("process") else ""
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  f"(Get-Process | Where-Object {{ $_.MainWindowTitle -like '*{c['title']}*'{proc} }}).Count"],
                                 capture_output=True, text=True).stdout.strip()
            if out in ("", "0") or time.monotonic() >= deadline:
                break
            time.sleep(1.0)
        return out in ("", "0"), f"no window titled *{c['title']}* left open"
    if kind == "max_steps":
        return len(run.get("steps", [])) <= c["n"], f"{len(run.get('steps', []))} steps <= {c['n']}"
    if kind == "tool_used":
        ok = any(t == c.get("name") or (c.get("prefix") and t.startswith(c["prefix"])) for t in tools)
        return ok, f"used {c.get('name') or c.get('prefix')}"
    if kind == "file_exists":
        return Path(c["path"]).is_file(), f"{c['path']} exists"
    if kind == "dir_missing":
        return not Path(c["path"]).exists(), f"{c['path']} removed"
    if kind == "file_count":
        n = len(list(Path(c["path"]).glob(c["pattern"].replace("\\", "/"))))
        return n == c["n"], f"{n} files match {c['pattern']} (want {c['n']})"
    if kind == "zip_contains":
        import zipfile
        try:
            names = {Path(n).name for n in zipfile.ZipFile(c["path"]).namelist()}
        except Exception as e:  # noqa: BLE001
            return False, f"zip unreadable: {e}"
        return set(c["names"]) <= names, f"zip has {c['names']}"
    if kind == "docx_contains":
        try:
            import docx
            text = "\n".join(p.text for p in docx.Document(c["path"]).paragraphs)
        except Exception as e:  # noqa: BLE001
            return False, f"document unreadable: {e}"
        return all(t in text for t in c["texts"]), f"document has {c['texts']}"
    if kind == "docx_absent":
        try:
            import docx
            text = " ".join(p.text for p in docx.Document(c["path"]).paragraphs)
        except Exception as e:  # noqa: BLE001
            return False, f"document unreadable: {e}"
        present = [t for t in c["texts"] if t in text]
        return not present, f"document no longer has {c['texts']} (still has {present})"
    if kind == "docx_table":
        try:
            import docx
            d = docx.Document(c["path"])
            cells = [cell.text.strip() for t in d.tables for row in t.rows for cell in row.cells]
        except Exception as e:  # noqa: BLE001
            return False, f"document unreadable: {e}"
        missing = [t for t in c["texts"] if t not in cells]
        return not missing and len(d.tables) >= 1, f"table cells have {c['texts']} (missing {missing})"
    if kind == "summary_bullets":
        lines = [l.strip() for l in (run.get("summary") or "").splitlines() if l.strip().startswith(("-", "•", "*", "+"))]
        words = [len(re.sub(r"^[-•*+]\s*", "", l).split()) for l in lines]
        ok = len(lines) == c["n"] and all(w <= c.get("max_words", 99) for w in words)
        return ok, f"{len(lines)} bullets of {words} words (want {c['n']} of <= {c.get('max_words', 99)})"
    if kind == "pptx_titles":
        try:
            import pptx
            slides = list(pptx.Presentation(c["path"]).slides)
            titles = [" ".join(s.shapes.title.text.split()) if s.shapes.title is not None else
                      " ".join(sh.text_frame.text for sh in s.shapes if sh.has_text_frame) for s in slides]
        except Exception as e:  # noqa: BLE001
            return False, f"presentation unreadable: {e}"
        ok = len(slides) == len(c["titles"]) and all(want in got for want, got in zip(c["titles"], titles))
        return ok, f"slides {titles} == {c['titles']}"
    if kind == "csv_rows":
        p = Path(c["path"])
        if not p.is_file():
            return False, f"{p} missing"
        rows = [r for r in csv.reader(p.read_text(encoding="utf-8-sig").splitlines()) if any(x.strip() for x in r)]
        return len(rows) == c["rows"], f"{len(rows)} CSV rows == {c['rows']}"
    if kind == "pdf_valid":
        p = Path(c["path"])
        return p.is_file() and p.read_bytes()[:5] == b"%PDF-", f"{p.name} is a PDF"
    if kind == "dir_exists":
        return Path(c["path"]).is_dir(), f"{c['path']} is a folder"
    if kind == "file_missing":
        return not Path(c["path"]).exists(), f"{c['path']} absent"
    if kind in ("file_contains", "file_regex", "file_not_contains"):
        p = Path(c["path"])
        if not p.is_file():
            return False, f"{p} missing"
        raw = p.read_bytes()
        text = next((raw.decode(enc) for enc in ("utf-8-sig", "utf-16") if _decodes(raw, enc)), raw.decode("latin-1"))
        if kind == "file_contains":
            return c["text"] in text, f"{p.name} contains {c['text']!r}"
        if kind == "file_not_contains":
            return c["text"] not in text, f"{p.name} no longer contains {c['text']!r}"
        return re.search(c["pattern"], text) is not None, f"{p.name} matches {c['pattern']}"
    if kind == "json_equals":
        try:
            data = json.loads(Path(c["path"]).read_text(encoding="utf-8-sig"))
        except Exception as e:  # noqa: BLE001
            return False, f"invalid JSON: {e}"
        return all(data.get(k) == v for k, v in c["value"].items()), f"JSON has {c['value']}"
    if kind in ("xlsx", "xlsx_rows", "xlsx_has"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(c["path"])
        except Exception as e:  # noqa: BLE001
            return False, f"workbook unreadable: {e}"
        ws = wb.worksheets[0]
        if kind == "xlsx_rows":
            return ws.max_row == c["rows"], f"{ws.max_row} rows == {c['rows']}"
        if kind == "xlsx_has":
            values = [v for row in ws.iter_rows(values_only=True) for v in row if v is not None]
            missing = [w for w in c["values"] if not any(v == w or str(v).strip() == str(w) for v in values)]
            return not missing, f"workbook has {c['values']}"
        for ref, want in c.get("cells", {}).items():
            got = ws[ref].value
            if isinstance(want, (int, float)):
                try:
                    if abs(float(got) - want) > 1e-9:
                        return False, f"{ref} = {got!r}, expected {want}"
                except (TypeError, ValueError):
                    return False, f"{ref} = {got!r}, expected {want}"
            elif str(got).strip() != want:
                return False, f"{ref} = {got!r}, expected {want!r}"
        for ref, want in c.get("formula", {}).items():
            got = str(ws[ref].value or "").replace(" ", "").upper()
            if got != "=" + want.upper():
                return False, f"{ref} = {ws[ref].value!r}, expected ={want}"
        return True, "workbook cells and formulas as asked"
    if kind == "summary_ram_gb":
        out = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"],
                             capture_output=True, text=True).stdout.strip()
        actual = int(out) / 1024**3
        return any(abs(n - actual) <= c["tolerance"] for n in numbers(summary)), f"RAM ~{actual:.1f} GB in answer"
    if kind == "summary_disk_free_gb":
        free = shutil.disk_usage(f"{c['drive']}:\\").free / 1024**3
        return any(abs(n - free) <= c["tolerance"] for n in numbers(summary)), f"free ~{free:.1f} GB in answer"
    if kind == "summary_today":
        today = dt.date.today()
        weekday = WEEKDAYS_VI[today.weekday()]
        day_ok = re.search(rf"\b0?{today.day}\b", summary) is not None
        return weekday in low and day_ok, f"answer says {weekday}, day {today.day}"
    if kind == "summary_catalog_count":
        db = ROOT / "services" / "company-brain" / "data" / "catalog.db"
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            actual = conn.execute(c["sql"]).fetchone()[0]
        return actual in [int(n) for n in numbers(summary) if n.is_integer()], f"answer has the real count {actual}"
    if kind == "honest_failure":
        honest = run["status"] != "completed" or any(a.lower() in low for a in c["any"])
        return honest, "did not pretend to succeed"
    raise ValueError(f"unknown check kind {kind}")


def _decodes(raw: bytes, enc: str) -> bool:
    try:
        raw.decode(enc)
        return True
    except UnicodeDecodeError:
        return False


# ----------------------------------------------------------------------------------------------- running

def autocad_running() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq acad.exe"], capture_output=True, text=True).stdout
    return "acad.exe" in out


def run_task(task: dict, env_knobs: dict[str, str]) -> dict:
    folder = WORK / task["id"]
    prepare(task, folder)
    prompt = expand(task["prompt"], folder)
    env = {**os.environ, **env_knobs, "PYTHONIOENCODING": "utf-8"}
    started = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run([str(AGENT), "run", "--mode", "full_access", prompt], cwd=ROOT, env=env, capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=TASK_TIMEOUT_S, encoding="utf-8", errors="replace")
        output = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as e:
        timed_out = True
        output = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
    duration = time.monotonic() - started
    m = re.search(r"^Task (\S+) \[", output, re.M)
    task_id = m.group(1) if m else None
    record = {}
    if task_id:
        path = ROOT / "memory" / "tasks" / f"{task_id}.json"
        if path.is_file():
            record = json.loads(path.read_text(encoding="utf-8"))
    steps = record.get("steps", [])
    llm = []
    if task_id and (ROOT / "logs" / "tasks" / f"{task_id}.jsonl").is_file():
        for line in (ROOT / "logs" / "tasks" / f"{task_id}.jsonl").read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("kind") == "llm":
                attempts = (event.get("detail") or {}).get("attempts") or []
                ok = next((a for a in attempts if a.get("ok")), None)
                llm.append({"backend": ok["backend"] if ok else None, "ms": event.get("duration_ms")})
    run = {
        "id": task["id"], "category": task["category"], "task_id": task_id,
        "status": "timeout" if timed_out else record.get("status", "unknown"),
        "summary": record.get("final_summary") or record.get("failure_reason") or "",
        "steps": [{"tool": s["tool"], "status": s["status"], "verification": s.get("verification"),
                   "error": (s.get("error") or {}).get("code") if isinstance(s.get("error"), dict) else s.get("error")} for s in steps],
        "duration_s": round(duration, 1), "llm_calls": len(llm), "llm": llm,
    }
    results = []
    for c in expand(task["checks"], folder):
        try:
            ok, detail = check(c, run)
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"check error: {e}"
        results.append({"kind": c["kind"], "ok": ok, "detail": detail})
    run["checks"] = results
    run["passed"] = all(r["ok"] for r in results)
    run["false_success"] = run["status"] == "completed" and not run["passed"]
    run["tool_errors"] = sum(1 for s in run["steps"] if s["status"] != "succeeded")
    run["recovered"] = run["passed"] and run["tool_errors"] > 0
    run["local_only"] = all(x["backend"] == "local" for x in llm) if llm else None
    cleanup(task)
    return run


def summarize(runs: list[dict]) -> dict:
    def rate(xs):
        return round(sum(xs) / len(xs), 3) if xs else None

    by_cat: dict[str, list[dict]] = {}
    for r in runs:
        by_cat.setdefault(r["category"], []).append(r)
    steps_total = sum(len(r["steps"]) for r in runs)
    return {
        "tasks": len(runs),
        "pass_rate": rate([r["passed"] for r in runs]),
        "false_successes": sum(r["false_success"] for r in runs),
        "timeouts": sum(r["status"] == "timeout" for r in runs),
        "recovered": sum(r["recovered"] for r in runs),
        "tool_error_rate": round(sum(r["tool_errors"] for r in runs) / steps_total, 3) if steps_total else 0.0,
        "median_duration_s": statistics.median([r["duration_s"] for r in runs]) if runs else None,
        "mean_llm_calls": round(statistics.mean([r["llm_calls"] for r in runs]), 2) if runs else None,
        "by_category": {k: rate([r["passed"] for r in v]) for k, v in sorted(by_cat.items())},
        "not_local": sum(1 for r in runs if r["local_only"] is False),
    }


def markdown(report: dict) -> str:
    s = report["summary"]
    lines = [f"# Benchmark {report['profile']} — {report['started']}", "",
             f"Knobs: `{json.dumps(report['knobs'])}`", "",
             f"- Pass rate: **{s['pass_rate']:.0%}** ({sum(r['passed'] for r in report['runs'])}/{s['tasks']})",
             f"- False successes (claimed done, checks failed): **{s['false_successes']}**",
             f"- Timeouts: {s['timeouts']} · recovered after tool errors: {s['recovered']} · tool error rate: {s['tool_error_rate']:.1%}",
             f"- Median time per task: {s['median_duration_s']} s · mean LLM calls: {s['mean_llm_calls']}",
             f"- Runs not served by the local model alone: {s['not_local']}", "",
             "| Category | Pass rate |", "|---|---|"]
    lines += [f"| {k} | {v:.0%} |" for k, v in s["by_category"].items()]
    lines += ["", "| Task | Result | Status | Time | Steps | Failed checks |", "|---|---|---|---|---|---|"]
    for r in report["runs"]:
        failed = "; ".join(c["detail"] for c in r["checks"] if not c["ok"])
        verdict = "PASS" if r["passed"] else ("FALSE SUCCESS" if r["false_success"] else "FAIL")
        lines.append(f"| {r['id']} | {verdict} | {r['status']} | {r['duration_s']} s | {len(r['steps'])} | {failed} |")
    return "\n".join(lines) + "\n"


def compare(paths: list[str]) -> None:
    reports = [json.loads(Path(p).read_text(encoding="utf-8")) for p in paths]
    keys = ["pass_rate", "false_successes", "timeouts", "tool_error_rate", "median_duration_s", "mean_llm_calls"]
    print("| metric | " + " | ".join(r["profile"] for r in reports) + " |")
    print("|---|" + "---|" * len(reports))
    for k in keys:
        print(f"| {k} | " + " | ".join(str(r["summary"][k]) for r in reports) + " |")
    ids = [r["id"] for r in reports[0]["runs"]]
    print("\n| task | " + " | ".join(r["profile"] for r in reports) + " |")
    print("|---|" + "---|" * len(reports))
    for i in dict.fromkeys(ids):
        cells = []
        for rep in reports:
            rs = [r for r in rep["runs"] if r["id"] == i]
            cells.append(f"{sum(r['passed'] for r in rs)}/{len(rs)}" if rs else "-")
        print(f"| {i} | " + " | ".join(cells) + " |")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="baseline", choices=sorted(PROFILES))
    ap.add_argument("--only", default="")
    ap.add_argument("--skip", default="")
    ap.add_argument("--categories", default="", help="comma-separated categories to run")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--compare", nargs="+")
    args = ap.parse_args()
    if args.compare:
        compare(args.compare)
        return 0
    tasks = [t for f in TASK_FILES for t in json.loads(f.read_text(encoding="utf-8"))["tasks"]]
    ap_categories = {t for t in args.categories.split(",") if t}
    if ap_categories:
        tasks = [t for t in tasks if t["category"] in ap_categories]
    only = {t for t in args.only.split(",") if t}
    skip = {t for t in args.skip.split(",") if t}
    tasks = [t for t in tasks if (not only or t["id"] in only) and t["id"] not in skip]
    knobs = PROFILES[args.profile]
    started = dt.datetime.now()
    runs = []
    for rep in range(args.repeat):
        for task in tasks:
            run = run_task(task, knobs)
            run["repeat"] = rep
            runs.append(run)
            verdict = "PASS" if run["passed"] else ("FALSE SUCCESS" if run["false_success"] else "FAIL")
            print(f"[{rep + 1}/{args.repeat}] {task['id']:16s} {verdict:13s} {run['status']:10s} {run['duration_s']:6.1f}s "
                  f"steps={len(run['steps'])} llm={run['llm_calls']}", flush=True)
    report = {"profile": args.profile, "knobs": knobs, "started": started.isoformat(timespec="seconds"), "runs": runs,
              "summary": summarize(runs)}
    REPORTS.mkdir(parents=True, exist_ok=True)
    stem = REPORTS / f"{started:%Y%m%d-%H%M%S}-{args.profile}"
    stem.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    stem.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    print(markdown(report))
    print(f"report: {stem}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
