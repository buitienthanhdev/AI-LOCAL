"""OfficeAuditLog (spec section 11) and the learning-record schema (spec section 13).

Audit log: one JSON object per line in <logs_dir>/office/audit-YYYY-MM-DD.jsonl. Every entry carries provenance
(who/what asked, which tool/task/step, which file and its hash), params and results are redacted (no passwords or
tokens are ever written) and the entries are hash-chained, so a deleted or edited line is detectable with `verify_chain`.

Learning records: a lesson is stored only when it is verified by evidence that did not come from the model's own
answer. Unverified or self-generated "success" is refused. A lesson seen once is a candidate, not a rule.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lga_worker.office.guard import redact, sha256_file

GENESIS = "0" * 64
MAX_HASH_BYTES = 200 * 1024 * 1024


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


class AuditLog:
    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self._lock = threading.Lock()
        self._last: dict[str, str] = {}

    def _file(self, when: datetime) -> Path:
        return self.dir / f"audit-{when:%Y-%m-%d}.jsonl"

    def _last_hash(self, f: Path) -> str:
        key = str(f)
        if key in self._last:
            return self._last[key]
        last = GENESIS
        if f.is_file():
            with f.open("rb") as fh:
                lines = fh.read().splitlines()
            for raw in reversed(lines):
                try:
                    last = json.loads(raw)["hash"]
                    break
                except (ValueError, KeyError):
                    continue
        self._last[key] = last
        return last

    def record(self, action: str, *, level: int | None = None, decision: str | None = None, status: str = "", application: str = "",
               target: str | None = None, params: Any = None, result: Any = None, provenance: dict[str, Any] | None = None,
               hash_target: bool = False) -> dict[str, Any]:
        """Append one entry and return it. A failure to write the log is raised, never swallowed here."""
        when = _now()
        entry: dict[str, Any] = {
            "id": uuid.uuid4().hex[:16], "at": when.isoformat(timespec="milliseconds"), "action": action, "application": application,
            "level": level, "decision": decision, "status": status, "target": target,
            "provenance": redact(provenance or {}), "params": redact(params if params is not None else {}), "result": redact(result if result is not None else {}),
        }
        if target and hash_target:
            tp = Path(target)
            try:
                if tp.is_file() and tp.stat().st_size <= MAX_HASH_BYTES:
                    entry["target_sha256"] = sha256_file(tp)
                    entry["target_size"] = tp.stat().st_size
            except OSError as exc:
                entry["target_hash_error"] = type(exc).__name__
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            f = self._file(when)
            entry["prev"] = self._last_hash(f)
            entry["hash"] = hashlib.sha256((entry["prev"] + _canon({k: v for k, v in entry.items() if k != "hash"})).encode("utf-8")).hexdigest()
            with f.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            self._last[str(f)] = entry["hash"]
        return entry

    def files(self) -> list[Path]:
        return sorted(self.dir.glob("audit-*.jsonl")) if self.dir.is_dir() else []

    def tail(self, limit: int = 20, action: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for f in reversed(self.files()):
            for raw in reversed(f.read_text(encoding="utf-8").splitlines()):
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if action and action not in e.get("action", ""):
                    continue
                out.append(e)
                if len(out) >= limit:
                    return out
        return out

    def verify_chain(self) -> dict[str, Any]:
        """Re-compute the hash chain of every file. ok=False names the first broken line."""
        checked = 0
        for f in self.files():
            prev = GENESIS
            for n, raw in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                try:
                    e = json.loads(raw)
                except ValueError:
                    return {"ok": False, "file": f.name, "line": n, "problem": "line is not valid JSON", "checked": checked}
                want = hashlib.sha256((prev + _canon({k: v for k, v in e.items() if k != "hash"})).encode("utf-8")).hexdigest()
                if e.get("prev") != prev or e.get("hash") != want:
                    return {"ok": False, "file": f.name, "line": n, "problem": "hash chain broken (line edited, removed or reordered)", "checked": checked}
                prev = e["hash"]
                checked += 1
        return {"ok": True, "checked": checked}


# ----------------------------------------------------------------------------------------- learning records
VERIFICATION_RESULTS = ("verified", "failed", "unverified")
SELF_GENERATED_SOURCES = {"model", "model_output", "assistant", "assistant_answer", "ai", "llm", "self", "agent", "agent_claim", "chat", "self_generated"}
TRUSTED_SOURCES = {"tool_result", "verification_engine", "test_run", "user_feedback", "human_review", "audit_log", "file_check"}
REQUIRED_TEXT = ("task_type", "application", "environment", "observed_failure", "root_cause", "corrective_action", "verification_result",
                 "provenance", "regression_test_reference")


class LearningRefused(ValueError):
    """The record does not meet the rules of spec section 13."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass
class LearningRecord:
    task_type: str
    application: str
    environment: str
    observed_failure: str
    root_cause: str
    corrective_action: str
    verification_result: str
    confidence: float
    provenance: dict[str, Any]
    regression_test_reference: str
    timestamp: str = field(default_factory=lambda: _now().isoformat(timespec="seconds"))

    def problems(self) -> list[str]:
        out: list[str] = []
        for name in ("task_type", "application", "environment", "observed_failure", "root_cause", "corrective_action", "regression_test_reference"):
            v = getattr(self, name)
            if not isinstance(v, str) or not v.strip():
                out.append(f"{name} is required")
        if self.verification_result not in VERIFICATION_RESULTS:
            out.append(f"verification_result must be one of {VERIFICATION_RESULTS}")
        if not isinstance(self.confidence, (int, float)) or isinstance(self.confidence, bool) or not 0.0 <= float(self.confidence) <= 1.0:
            out.append("confidence must be a number between 0 and 1")
        if not isinstance(self.provenance, dict) or not self.provenance.get("source"):
            out.append("provenance.source is required (where the evidence came from)")
        else:
            src = str(self.provenance["source"]).strip().lower()
            if src in SELF_GENERATED_SOURCES:
                out.append(f"provenance.source={src!r} is the AI's own output; an AI answer is not evidence of success")
            elif src not in TRUSTED_SOURCES:
                out.append(f"provenance.source={src!r} is not a recognised evidence source ({sorted(TRUSTED_SOURCES)})")
            if not self.provenance.get("evidence"):
                out.append("provenance.evidence is required (file path, audit entry id, test name or tool result reference)")
        try:
            datetime.fromisoformat(self.timestamp)
        except (TypeError, ValueError):
            out.append("timestamp must be an ISO-8601 string")
        return out

    def validate(self) -> None:
        problems = self.problems()
        if problems:
            raise LearningRefused(problems)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LearningRecord":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        extra = sorted(set(data) - known)
        if extra:
            raise LearningRefused([f"unknown field(s): {extra}"])
        missing = [k for k in REQUIRED_TEXT + ("confidence",) if k not in data]
        if missing:
            raise LearningRefused([f"missing field(s): {missing}"])
        return cls(**data)


class LearningStore:
    """Append-only JSONL. `store` refuses unverified/self-generated records; repeated lessons count occurrences."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _all(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        out = []
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(raw))
            except ValueError:
                continue
        return out

    @staticmethod
    def _key(rec: dict[str, Any]) -> tuple[str, str, str]:
        norm = lambda s: re.sub(r"\s+", " ", str(s).strip().lower())  # noqa: E731
        return norm(rec["task_type"]), norm(rec["application"]), norm(rec["root_cause"])

    def store(self, record: LearningRecord | dict[str, Any]) -> dict[str, Any]:
        rec = record if isinstance(record, LearningRecord) else LearningRecord.from_dict(record)
        problems = rec.problems()
        if rec.verification_result != "verified":
            problems.append(f"verification_result={rec.verification_result!r}: only verified outcomes are stored as lessons "
                            "(an unverified or failed attempt is not a reliable lesson)")
        if problems:
            raise LearningRefused(problems)
        data = redact(asdict(rec), max_str=2000)
        data["outcome"] = "verified_success"
        data["status"] = "pending_regression" if rec.regression_test_reference.strip().upper().startswith("PENDING") else "candidate"
        with self._lock:
            existing = [e for e in self._all() if e.get("outcome") == "verified_success" and self._key(e) == self._key(data)]
            occurrences = 1 + len(existing)
            data["occurrences"] = occurrences
            data["generalizable"] = occurrences >= 2 and data["status"] != "pending_regression"
            data["applied"] = False  # nothing changes production behaviour from here; a reviewed change + regression test does
            data["note"] = ("seen once: kept as a candidate, not a rule" if occurrences < 2 else f"seen {occurrences} times with verified evidence")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(data, ensure_ascii=False) + "\n")
        return data

    def store_failed_strategy(self, task_type: str, application: str, corrective_action: str, provenance: dict[str, Any], reason: str) -> dict[str, Any]:
        """Remember that a strategy was VERIFIED to fail (evidence required), so it is not repeated unchanged."""
        src = str(provenance.get("source", "")).strip().lower()
        if src in SELF_GENERATED_SOURCES or src not in TRUSTED_SOURCES or not provenance.get("evidence"):
            raise LearningRefused(["a failed strategy needs trusted provenance with evidence (not the AI's own claim)"])
        if not (task_type.strip() and application.strip() and corrective_action.strip()):
            raise LearningRefused(["task_type, application and corrective_action are required"])
        data = redact({"task_type": task_type, "application": application, "corrective_action": corrective_action, "reason": reason,
                       "provenance": provenance, "timestamp": _now().isoformat(timespec="seconds"), "outcome": "failed", "status": "failed_strategy"}, max_str=2000)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(data, ensure_ascii=False) + "\n")
        return data

    def strategy_failed_before(self, task_type: str, application: str, corrective_action: str) -> list[dict[str, Any]]:
        """Records of this exact strategy that were verified to have FAILED, so it is not repeated unchanged."""
        want = re.sub(r"\s+", " ", corrective_action.strip().lower())
        return [e for e in self._all() if e.get("task_type") == task_type and e.get("application") == application
                and re.sub(r"\s+", " ", str(e.get("corrective_action", "")).strip().lower()) == want and e.get("outcome") == "failed"]

    def all(self) -> list[dict[str, Any]]:
        return self._all()
