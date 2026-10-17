"""OfficeWorkflowEngine core (spec sections 8, 12, 14): statuses, dependency graph, checkpoint/resume, retry policy.

This is a state machine and a planner, not an executor: the agent (or a caller) runs each step with the real tools
and reports the outcome through `start / succeed / fail / ...`. The engine's job is to refuse what the spec forbids:

* SUCCEEDED needs evidence - a "success" without evidence is stored as UNKNOWN;
* a non-idempotent step (send mail, create a record/draft/event, append) is never retried blindly - after a failure or
  a timeout the real state is checked first (`reconcile`), and only "verified not done" allows another attempt;
* an interrupted RUNNING step found in a checkpoint becomes UNKNOWN, never SUCCEEDED;
* a step needing the network is "must wait" offline, and nothing is simulated;
* a step with no adapter in the capability registry cannot be marked runnable.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
MAX_HISTORY = 500


class Status(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


S = Status
TRANSIENT = {"timeout", "locked", "busy", "rate_limited", "network", "transient", "app_hung"}
PERMANENT = {"invalid_argument", "not_found", "permission_denied", "corrupt", "encrypted", "validation", "unsupported", "disk_full"}
CLOUD_APPS = {"onedrive_sharepoint", "teams", "forms", "lists_planner", "power_automate", "power_bi", "power_apps", "graph"}
NET_TOKENS = {"send", "forward", "reply", "sync", "share", "invite", "publish", "upload", "refresh", "download_cloud", "deliver"}
NON_IDEMPOTENT_TOKENS = {"send", "forward", "reply", "create", "append", "post", "insert", "add", "upload", "invite", "publish", "draft",
                         "schedule", "share", "submit", "book"}
IDEMPOTENT_TOKENS = {"read", "list", "search", "get", "detect", "verify", "check", "export", "convert", "extract", "analyse", "analyze",
                     "aggregate", "gather", "compare", "dedupe", "normalise", "normalize", "summarise", "summarize", "write", "replace", "capabilities"}


class WorkflowError(Exception):
    """An operation the state machine refuses (illegal transition, bad graph, retry not allowed)."""

    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _tokens(op: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", op.lower()) if t}


def infer_idempotent(operation: str) -> bool:
    """Conservative: unknown operations are NOT idempotent (so they are never retried blindly)."""
    toks = _tokens(operation)
    if toks & NON_IDEMPOTENT_TOKENS:
        return False
    return bool(toks & IDEMPOTENT_TOKENS)


def infer_needs_network(operation: str, application: str = "") -> bool:
    toks = _tokens(operation)
    return application.lower() in CLOUD_APPS or bool(toks & NET_TOKENS) or operation.lower().startswith(("graph.", "m365."))


@dataclass
class Step:
    id: str
    name: str
    operation: str
    application: str = ""
    depends_on: list[str] = field(default_factory=list)
    idempotent: bool | None = None
    needs_network: bool | None = None
    requires_confirmation: bool = False
    accept_partial_dependency: bool = False
    max_attempts: int = 3
    verify: dict[str, Any] | None = None  # how the real state can be checked, e.g. {"kind": "file_valid", "path": ..., "type": "pdf"}
    supported: bool | None = None  # from the capability registry; False = no adapter, cannot run
    support_reason: str = ""
    # --- runtime state
    status: str = Status.PENDING.value
    attempts: int = 0
    approved: bool = False
    verified_not_done: bool = False
    error: str = ""
    error_kind: str = ""
    question: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    done_items: list[str] = field(default_factory=list)
    remaining_items: list[str] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None

    def __post_init__(self) -> None:
        if not ID_RE.match(self.id):
            raise WorkflowError(f"step id {self.id!r} must match {ID_RE.pattern}")
        if self.idempotent is None:
            self.idempotent = infer_idempotent(self.operation)
        if self.needs_network is None:
            self.needs_network = infer_needs_network(self.operation, self.application)
        Status(self.status)  # validates


class Workflow:
    def __init__(self, workflow_id: str, title: str, steps: list[Step], *, meta: dict[str, Any] | None = None) -> None:
        if not ID_RE.match(workflow_id):
            raise WorkflowError(f"workflow id {workflow_id!r} must match {ID_RE.pattern}")
        self.id = workflow_id
        self.title = title
        self.steps: dict[str, Step] = {}
        for st in steps:
            if st.id in self.steps:
                raise WorkflowError(f"duplicate step id {st.id}")
            self.steps[st.id] = st
        self.meta = dict(meta or {})
        self.created_at = now_iso()
        self.updated_at = self.created_at
        self.cancelled = False
        self.history: list[dict[str, Any]] = []
        self.path: Path | None = None
        problems = self.validate()
        if problems:
            raise WorkflowError("invalid workflow graph: " + "; ".join(problems), {"problems": problems})

    # ------------------------------------------------------------------ graph
    def validate(self) -> list[str]:
        problems = []
        for st in self.steps.values():
            for dep in st.depends_on:
                if dep not in self.steps:
                    problems.append(f"{st.id} depends on unknown step {dep}")
                if dep == st.id:
                    problems.append(f"{st.id} depends on itself")
        cycle = self.find_cycle()
        if cycle:
            problems.append("dependency cycle: " + " -> ".join(cycle))
        return problems

    def find_cycle(self) -> list[str] | None:
        color: dict[str, int] = {}
        stack: list[str] = []

        def visit(n: str) -> list[str] | None:
            color[n] = 1
            stack.append(n)
            for d in self.steps[n].depends_on:
                if d not in self.steps:
                    continue
                if color.get(d) == 1:
                    return stack[stack.index(d):] + [d]
                if color.get(d) is None:
                    found = visit(d)
                    if found:
                        return found
            stack.pop()
            color[n] = 2
            return None

        for n in self.steps:
            if color.get(n) is None:
                found = visit(n)
                if found:
                    return found
        return None

    def order(self) -> list[str]:
        """A topological order (stable: plan order breaks ties)."""
        done: list[str] = []
        left = list(self.steps)
        while left:
            nxt = next((n for n in left if all(d in done for d in self.steps[n].depends_on)), None)
            if nxt is None:
                raise WorkflowError("dependency cycle")
            done.append(nxt)
            left.remove(nxt)
        return done

    def _deps_ok(self, st: Step) -> bool:
        for d in st.depends_on:
            ds = self.steps[d]
            if ds.status == S.SUCCEEDED.value:
                continue
            if ds.status == S.PARTIAL.value and st.accept_partial_dependency:
                continue
            return False
        return True

    def ready(self) -> list[Step]:
        if self.cancelled:
            return []
        return [st for st in self.steps.values() if st.status == S.PENDING.value and st.supported is not False and self._deps_ok(st)
                and not (st.requires_confirmation and not st.approved)]

    def needs_confirmation(self) -> list[Step]:
        return [st for st in self.steps.values() if st.status == S.PENDING.value and st.requires_confirmation and not st.approved
                and st.supported is not False and self._deps_ok(st)]

    def blocked(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for st in self.steps.values():
            if st.status != S.PENDING.value:
                continue
            if st.supported is False:
                out[st.id] = f"no adapter: {st.support_reason or 'operation not supported'}"
                continue
            bad = [d for d in st.depends_on if self.steps[d].status in (S.FAILED.value, S.CANCELLED.value, S.UNKNOWN.value, S.PARTIAL.value)
                   and not (self.steps[d].status == S.PARTIAL.value and st.accept_partial_dependency)]
            waiting = [d for d in st.depends_on if self.steps[d].status not in (S.SUCCEEDED.value,) and d not in bad]
            if bad:
                out[st.id] = "blocked by " + ", ".join(f"{d} ({self.steps[d].status})" for d in bad)
            elif waiting and all(self.steps[d].status in (S.PENDING.value, S.RUNNING.value, S.WAITING_FOR_USER.value) for d in waiting):
                pass  # simply not its turn yet
        return out

    # ------------------------------------------------------------------ transitions
    def _log(self, step: Step | None, old: str | None, new: str | None, note: str = "") -> None:
        self.history.append({"at": now_iso(), "step": step.id if step else None, "from": old, "to": new, "note": note[:300]})
        del self.history[:-MAX_HISTORY]
        self.updated_at = now_iso()

    def _set(self, st: Step, new: Status, note: str = "") -> None:
        old = st.status
        st.status = new.value
        self._log(st, old, new.value, note)

    def _step(self, step_id: str) -> Step:
        if step_id not in self.steps:
            raise WorkflowError(f"unknown step {step_id}")
        return self.steps[step_id]

    def _expect(self, st: Step, *allowed: Status) -> None:
        if st.status not in {a.value for a in allowed}:
            raise WorkflowError(f"step {st.id} is {st.status}; expected {' or '.join(a.value for a in allowed)}")

    def start(self, step_id: str) -> Step:
        st = self._step(step_id)
        self._expect(st, S.PENDING)
        if self.cancelled:
            raise WorkflowError("workflow is cancelled")
        if st.supported is False:
            raise WorkflowError(f"step {st.id} cannot run: no adapter ({st.support_reason})")
        if not self._deps_ok(st):
            raise WorkflowError(f"step {st.id} is not ready: dependencies not satisfied", {"blocked": self.blocked().get(st.id, "waiting for dependencies")})
        if st.requires_confirmation and not st.approved:
            raise WorkflowError(f"step {st.id} needs the user's confirmation first (wait_for_user)")
        st.attempts += 1
        st.started_at = now_iso()
        st.finished_at = None
        st.error = st.error_kind = ""
        st.verified_not_done = False
        self._set(st, S.RUNNING, f"attempt {st.attempts}")
        self._commit()
        return st

    def succeed(self, step_id: str, evidence: list[dict[str, Any]] | dict[str, Any] | str | None = None, note: str = "") -> Step:
        st = self._step(step_id)
        self._expect(st, S.RUNNING, S.UNKNOWN)
        ev = _norm_evidence(evidence)
        st.finished_at = now_iso()
        if not ev:
            st.error = "reported success without evidence"
            self._set(st, S.UNKNOWN, "success claimed without any evidence: not accepted")
        else:
            st.evidence.extend(ev)
            st.error = ""
            self._set(st, S.SUCCEEDED, note)
        self._commit()
        return st

    def fail(self, step_id: str, error: str, error_kind: str = "", evidence: Any = None) -> dict[str, Any]:
        st = self._step(step_id)
        self._expect(st, S.RUNNING)
        st.error, st.error_kind = error[:500], error_kind
        st.finished_at = now_iso()
        st.evidence.extend(_norm_evidence(evidence))
        # A timeout says nothing about whether the action happened: that is UNKNOWN, not FAILED.
        if error_kind == "timeout":
            self._set(st, S.UNKNOWN, "timeout: the real state must be checked before anything else")
        else:
            self._set(st, S.FAILED, error[:200])
        self._commit()
        return self.retry_decision(step_id)

    def partial(self, step_id: str, done: list[str], remaining: list[str], evidence: Any = None) -> Step:
        st = self._step(step_id)
        self._expect(st, S.RUNNING)
        if not remaining:
            raise WorkflowError("PARTIAL needs the list of remaining items; use succeed() when nothing remains")
        st.done_items, st.remaining_items = list(done), list(remaining)
        st.evidence.extend(_norm_evidence(evidence))
        st.finished_at = now_iso()
        self._set(st, S.PARTIAL, f"{len(done)} done, {len(remaining)} remaining")
        self._commit()
        return st

    def unknown(self, step_id: str, reason: str) -> Step:
        st = self._step(step_id)
        self._expect(st, S.RUNNING, S.PARTIAL, S.FAILED)
        st.error = reason[:500]
        self._set(st, S.UNKNOWN, reason)
        self._commit()
        return st

    def wait_for_user(self, step_id: str, question: str) -> Step:
        st = self._step(step_id)
        self._expect(st, S.PENDING, S.RUNNING)
        st.question = question
        self._set(st, S.WAITING_FOR_USER, question[:200])
        self._commit()
        return st

    def user_decision(self, step_id: str, approve: bool, note: str = "") -> Step:
        st = self._step(step_id)
        self._expect(st, S.WAITING_FOR_USER)
        if approve:
            st.approved = True
            st.question = ""
            self._set(st, S.PENDING, "user approved " + note)
        else:
            self._set(st, S.CANCELLED, "user declined " + note)
            self._cancel_dependents(st.id)
        self._commit()
        return st

    def _cancel_dependents(self, step_id: str) -> None:
        for other in self.steps.values():
            if step_id in other.depends_on and other.status == S.PENDING.value:
                self._set(other, S.CANCELLED, f"dependency {step_id} cancelled")
                self._cancel_dependents(other.id)

    def cancel(self, step_id: str | None = None) -> dict[str, Any]:
        """Cancel one step (and what depends on it) or the whole workflow. A RUNNING step cannot be stopped from here."""
        running: list[str] = []
        targets = [self._step(step_id)] if step_id else list(self.steps.values())
        if not step_id:
            self.cancelled = True
        for st in targets:
            if st.status == S.RUNNING.value:
                running.append(st.id)
            elif st.status in (S.PENDING.value, S.WAITING_FOR_USER.value):
                self._set(st, S.CANCELLED, "cancelled")
                self._cancel_dependents(st.id)
        self._commit()
        return {"cancelled": [s.id for s in self.steps.values() if s.status == S.CANCELLED.value], "still_running": running,
                "note": "a RUNNING step is not interrupted by this engine; wait for its result or reconcile it" if running else ""}

    # ------------------------------------------------------------------ retry / recovery
    def retry_decision(self, step_id: str, error_kind: str | None = None) -> dict[str, Any]:
        st = self._step(step_id)
        kind = error_kind if error_kind is not None else st.error_kind
        base = {"step": st.id, "status": st.status, "idempotent": bool(st.idempotent), "attempts": st.attempts, "error_kind": kind}
        if st.status not in (S.FAILED.value, S.UNKNOWN.value, S.PARTIAL.value):
            return {**base, "action": "none", "reason": f"step is {st.status}; nothing to retry"}
        if st.verified_not_done and st.attempts < st.max_attempts:
            return {**base, "action": "retry", "backoff_s": 0, "reason": "the real state was checked and the action did NOT happen; one more attempt is safe"}
        if st.attempts >= st.max_attempts:
            return {**base, "action": "no_retry", "reason": f"{st.attempts} attempts used (max {st.max_attempts}); report the failure"}
        if kind in PERMANENT:
            return {**base, "action": "no_retry", "reason": f"{kind} is not a transient error; retrying cannot help"}
        if st.status == S.PARTIAL.value:
            if st.idempotent:
                return {**base, "action": "retry", "backoff_s": 0, "reason": "partly done and idempotent: continue with the remaining items only"}
            return {**base, "action": "check_state_first", "can_check_locally": bool(st.verify),
                    "reason": "partly done and not idempotent: check what was already created/sent before continuing"}
        if st.status == S.UNKNOWN.value or kind == "timeout":
            return {**base, "action": "check_state_first", "can_check_locally": bool(st.verify),
                    "reason": "outcome unknown after a timeout/interruption: check what really happened (reconcile) before any retry"}
        if kind == "locked":
            return {**base, "action": "wait_bounded_or_copy", "reason": "file/workbook is locked: wait a bounded time or work on a copy"}
        if kind == "network" and st.needs_network:
            return {**base, "action": "wait_online", "reason": "needs the network; do not simulate the result"}
        if kind in TRANSIENT:
            if st.idempotent:
                return {**base, "action": "retry", "backoff_s": min(30.0, 0.5 * 2 ** max(0, st.attempts - 1)),
                        "reason": "transient error on an idempotent step"}
            return {**base, "action": "check_state_first", "can_check_locally": bool(st.verify),
                    "reason": "step is not idempotent (could duplicate a mail/record/event): never retried blindly"}
        return {**base, "action": "no_retry", "reason": "error kind unknown or not transient; not retried automatically"}

    def retry(self, step_id: str, error_kind: str | None = None) -> Step:
        """FAILED/PARTIAL -> PENDING, only when the retry policy allows it."""
        st = self._step(step_id)
        decision = self.retry_decision(step_id, error_kind)
        if decision["action"] != "retry":
            raise WorkflowError(f"retry of {step_id} refused: {decision['reason']}", decision)
        if st.status == S.UNKNOWN.value and not st.verified_not_done:
            raise WorkflowError(f"retry of {step_id} refused: state unknown", decision)
        self._set(st, S.PENDING, "retry allowed: " + decision["reason"])
        self._commit()
        return st

    def reconcile(self, step_id: str, observed: dict[str, Any] | None = None) -> dict[str, Any]:
        """Settle a FAILED/UNKNOWN/PARTIAL/RUNNING step from EVIDENCE of the real state.

        observed = {"done": True|False|None, "evidence": ...}. Without `observed`, the step's own `verify` spec is evaluated
        when it can be checked locally (file checks). done=None keeps/sets UNKNOWN.
        """
        st = self._step(step_id)
        self._expect(st, S.FAILED, S.UNKNOWN, S.PARTIAL, S.RUNNING)
        if observed is None:
            observed = check_locally(st.verify) if st.verify else {"done": None, "evidence": [{"source": "none", "detail": "no local check defined for this step"}]}
        done = observed.get("done")
        ev = _norm_evidence(observed.get("evidence"))
        st.evidence.extend(ev)
        if done is True and ev:
            st.error = ""
            st.finished_at = now_iso()
            self._set(st, S.SUCCEEDED, "reconciled: real state shows the action is done")
            result = "SUCCEEDED"
        elif done is False and ev:
            st.verified_not_done = True
            self._set(st, S.PENDING, "reconciled: real state shows the action did not happen")
            result = "PENDING"
        else:
            if st.status != S.UNKNOWN.value:
                self._set(st, S.UNKNOWN, "no evidence either way")
            result = "UNKNOWN"
        self._commit()
        return {"step": st.id, "result": result, "evidence": ev}

    # ------------------------------------------------------------------ views
    def overall(self) -> str:
        sts = [s.status for s in self.steps.values()]
        if not sts:
            return S.SUCCEEDED.value
        if all(x == S.SUCCEEDED.value for x in sts):
            return S.SUCCEEDED.value
        if self.cancelled:
            return S.CANCELLED.value
        if S.RUNNING.value in sts:
            return S.RUNNING.value
        if S.WAITING_FOR_USER.value in sts:
            return S.WAITING_FOR_USER.value
        started = any(x != S.PENDING.value for x in sts)
        if self.ready() or self.needs_confirmation():
            return S.RUNNING.value if started else S.PENDING.value
        if not started:
            return S.PENDING.value
        if any(x in (S.SUCCEEDED.value, S.PARTIAL.value) for x in sts):
            return S.PARTIAL.value
        if S.FAILED.value in sts:
            return S.FAILED.value
        return S.UNKNOWN.value

    def report(self) -> dict[str, Any]:
        by: dict[str, list[str]] = {s.value: [] for s in Status}
        for st in self.steps.values():
            by[st.status].append(st.id)
        remaining = [i for i in self.order() if self.steps[i].status != S.SUCCEEDED.value]
        return {"workflow_id": self.id, "title": self.title, "overall": self.overall(), "counts": {k: len(v) for k, v in by.items() if v},
                "completed_steps": by[S.SUCCEEDED.value], "remaining_steps": remaining, "by_status": {k: v for k, v in by.items() if v},
                "ready": [s.id for s in self.ready()], "needs_confirmation": [s.id for s in self.needs_confirmation()],
                "waiting_for_user": [{"step": s, "question": self.steps[s].question} for s in by[S.WAITING_FOR_USER.value]],
                "blocked": self.blocked(), "unsupported": {s.id: s.support_reason for s in self.steps.values() if s.supported is False},
                "unknown": [{"step": s, "why": self.steps[s].error} for s in by[S.UNKNOWN.value]],
                "partial": [{"step": s, "done": self.steps[s].done_items, "remaining": self.steps[s].remaining_items} for s in by[S.PARTIAL.value]],
                "failed": [{"step": s, "error": self.steps[s].error, "error_kind": self.steps[s].error_kind} for s in by[S.FAILED.value]],
                "cancelled": by[S.CANCELLED.value], "updated_at": self.updated_at}

    def resume_plan(self) -> dict[str, Any]:
        """What a resumed run does: skip finished steps, reconcile uncertain ones, run what is ready."""
        return {"skip_completed": [s.id for s in self.steps.values() if s.status == S.SUCCEEDED.value],
                "reconcile_first": [s.id for s in self.steps.values() if s.status in (S.UNKNOWN.value, S.PARTIAL.value, S.FAILED.value)],
                "run_next": [s.id for s in self.ready()], "waiting_for_user": [s.id for s in self.steps.values() if s.status == S.WAITING_FOR_USER.value],
                "blocked": self.blocked()}

    # ------------------------------------------------------------------ offline / online (section 14)
    def offline_plan(self, online: bool = False) -> dict[str, Any]:
        done = [s.id for s in self.steps.values() if s.status == S.SUCCEEDED.value]
        verdict: dict[str, tuple[str, str]] = {}
        for sid in self.order():
            st = self.steps[sid]
            if st.status in (S.SUCCEEDED.value, S.CANCELLED.value):
                continue
            if st.supported is False:
                verdict[sid] = ("must_wait", f"no adapter exists: {st.support_reason or 'unsupported'} (online or offline)")
                continue
            if st.needs_network and not online:
                why = "needs the network (mail delivery / cloud / sync): nothing is sent, synced or updated while offline; it is NOT simulated"
                verdict[sid] = ("must_wait", why)
                continue
            blockers = [d for d in st.depends_on if verdict.get(d, ("", ""))[0] == "must_wait"]
            if blockers:
                verdict[sid] = ("must_wait", "depends on " + ", ".join(blockers) + ", which must wait")
                continue
            verdict[sid] = ("can_continue", "local step: runs without a network" if not st.needs_network else "online: network available")
        return {"online": online, "done": done,
                "can_continue": [{"step": s, "reason": r} for s, (v, r) in verdict.items() if v == "can_continue"],
                "must_wait": [{"step": s, "reason": r} for s, (v, r) in verdict.items() if v == "must_wait"],
                "note": "" if online else "offline: local file work, parsers, verification and drafts continue; sending, cloud and sync wait"}

    # ------------------------------------------------------------------ persistence
    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "id": self.id, "title": self.title, "meta": self.meta, "created_at": self.created_at,
                "updated_at": self.updated_at, "cancelled": self.cancelled, "steps": [asdict(s) for s in self.steps.values()],
                "history": self.history}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Workflow":
        if data.get("schema_version") != SCHEMA_VERSION:
            raise WorkflowError(f"unsupported checkpoint schema_version {data.get('schema_version')!r}")
        steps = [Step(**s) for s in data["steps"]]
        wf = cls(data["id"], data.get("title", ""), steps, meta=data.get("meta"))
        wf.created_at, wf.updated_at = data.get("created_at", wf.created_at), data.get("updated_at", wf.updated_at)
        wf.cancelled = bool(data.get("cancelled"))
        wf.history = list(data.get("history", []))
        return wf

    def save(self, directory: str | Path | None = None) -> Path:
        if directory is not None:
            self.path = Path(directory) / f"{self.id}.json"
        if self.path is None:
            raise WorkflowError("no checkpoint path: pass a directory to save()")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
        return self.path

    def _commit(self) -> None:
        if self.path is not None:
            self.save()

    @classmethod
    def load(cls, path: str | Path) -> "Workflow":
        p = Path(path)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise WorkflowError(f"no checkpoint at {p}") from exc
        except ValueError as exc:
            raise WorkflowError(f"checkpoint {p} is damaged: {exc}") from exc
        wf = cls.from_dict(data)
        wf.path = p
        interrupted = [s for s in wf.steps.values() if s.status == S.RUNNING.value]
        for st in interrupted:  # the process ended while the step ran: we do not know whether it finished
            st.error = "interrupted while RUNNING (process ended); no evidence of the outcome"
            wf._set(st, S.UNKNOWN, st.error)
        if interrupted:
            wf._commit()
        return wf

    @classmethod
    def open(cls, directory: str | Path, workflow_id: str) -> "Workflow":
        if not ID_RE.match(workflow_id):
            raise WorkflowError(f"workflow id {workflow_id!r} is not valid")
        return cls.load(Path(directory) / f"{workflow_id}.json")


def list_workflows(directory: str | Path) -> list[dict[str, Any]]:
    out = []
    for f in sorted(Path(directory).glob("*.json")) if Path(directory).is_dir() else []:
        try:
            wf = Workflow.load(f)
            out.append({"workflow_id": wf.id, "title": wf.title, "overall": wf.overall(), "updated_at": wf.updated_at, "steps": len(wf.steps)})
        except WorkflowError as exc:
            out.append({"workflow_id": f.stem, "error": str(exc)})
    return out


def new_workflow_id(prefix: str = "wf") -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def _norm_evidence(evidence: Any) -> list[dict[str, Any]]:
    if not evidence:
        return []
    items = evidence if isinstance(evidence, list) else [evidence]
    out = []
    for it in items:
        if isinstance(it, dict) and it:
            out.append({"at": now_iso(), **it})
        elif isinstance(it, str) and it.strip():
            out.append({"at": now_iso(), "detail": it.strip()})
    return out


def check_locally(spec: dict[str, Any] | None) -> dict[str, Any]:
    """Evaluate a step's `verify` spec when the real state is on this computer. Anything else is UNKNOWN (done=None)."""
    if not spec:
        return {"done": None, "evidence": []}
    kind = spec.get("kind")
    if kind == "file_valid":
        from lga_worker.office import filetype

        info = filetype.detect(spec.get("path", ""))
        want = spec.get("type")
        ev = [{"source": "filetype.detect", "path": spec.get("path"), "status": info["status"], "detected_type": info["detected_type"],
               "size_bytes": info["size_bytes"]}]
        if info["status"] == "missing":
            return {"done": False, "evidence": ev}
        ok = info["status"] == "ok" and info["size_bytes"] >= int(spec.get("min_size", 1)) and (not want or info["detected_type"] == want)
        # a file that exists but is damaged/the wrong type is not proof either way for a step that was interrupted
        return {"done": True if ok else None, "evidence": ev}
    return {"done": None, "evidence": [{"source": "none", "detail": f"verify kind {kind!r} cannot be checked locally (needs the application/service)"}]}


# ----------------------------------------------------------------------------------------- templates (section 8)
def _step(id: str, name: str, op: str, app: str, deps: list[str] | None = None, **kw: Any) -> Step:
    return Step(id=id, name=name, operation=op, application=app, depends_on=deps or [], **kw)


def template_steps(template: str, params: dict[str, Any] | None = None) -> list[Step]:
    p = params or {}
    out_dir = str(p.get("output_dir", ""))

    def f(name: str) -> str:
        return str(Path(out_dir) / name) if out_dir else name

    if template == "weekly_report":
        steps = [
            _step("gather", "Collect data from the source documents", "excel.read_range", "excel"),
            _step("dedupe", "Cross-check and remove duplicates", "excel.aggregate", "excel", ["gather"]),
            _step("docx", "Create the DOCX report from the template", "word.write_text", "word", ["dedupe"],
                  verify={"kind": "file_valid", "path": f("report.docx"), "type": "docx"}),
            _step("xlsx", "Create the XLSX summary", "excel.write_range", "excel", ["dedupe"],
                  verify={"kind": "file_valid", "path": f("summary.xlsx"), "type": "xlsx"}),
        ]
        last = ["docx", "xlsx"]
        if p.get("with_pptx"):
            steps.append(_step("pptx", "Create the PowerPoint summary", "powerpoint.create_slides", "powerpoint", ["dedupe"],
                               verify={"kind": "file_valid", "path": f("summary.pptx"), "type": "pptx"}))
            last.append("pptx")
        steps += [
            _step("pdf", "Export the report to PDF", "word.export_pdf", "word", ["docx"], verify={"kind": "file_valid", "path": f("report.pdf"), "type": "pdf"}),
            _step("verify", "Check content AND layout of every produced file", "office.verify", "", last + ["pdf"]),
            _step("draft", "Create the e-mail DRAFT with the attachments", "outlook.create_draft", "outlook", ["verify"]),
            _step("send", "Send the e-mail (only after the user confirms)", "outlook.send_mail", "outlook", ["draft"], requires_confirmation=True),
        ]
        return steps
    if template == "project_plan":
        target = str(p.get("target", "planner")).lower()
        app = "project" if target == "project" else "lists_planner"
        steps = [
            _step("read", "Read the requirements and task list", "document.read", "word"),
            _step("normalise", "Normalise tasks and dependencies (check for dependency cycles)", "excel.aggregate", "excel", ["read"]),
            _step("plan", f"Build the plan in {'Project' if app == 'project' else 'Planner'}", f"{app}.update_plan" if app == "project" else "lists_planner.update_tasks",
                  app, ["normalise"], requires_confirmation=app != "project"),
            _step("tracker", "Create the tracking table in Excel", "excel.write_range", "excel", ["normalise"],
                  verify={"kind": "file_valid", "path": f("tracker.xlsx"), "type": "xlsx"}),
        ]
        if p.get("with_dashboard"):
            steps.append(_step("dashboard", "Create the Power BI dashboard", "power_bi.publish", "power_bi", ["tracker"], requires_confirmation=True))
        steps.append(_step("consistency", "Check consistency between the sources", "office.verify", "", ["plan", "tracker"]))
        return steps
    if template == "email_doc_analysis":
        return [
            _step("find", "Find the related e-mails", "outlook.list_mail", "outlook"),
            _step("read", "Read the e-mails and list the attachments", "outlook.read_mail", "outlook", ["find"]),
            _step("extract", "Extract the data from the permitted attachments and record its source", "excel.read_range", "excel", ["read"]),
            _step("compare", "Compare the versions of the documents", "word.compare_versions", "word", ["extract"]),
            _step("summarise", "Summarise into Excel or Word, facts apart from inference", "excel.write_range", "excel", ["compare"]),
            _step("verify", "Verify the summary against the sources", "office.verify", "", ["summarise"]),
        ]
    raise WorkflowError(f"unknown template {template!r}; known: weekly_report, project_plan, email_doc_analysis")


TEMPLATES = ("weekly_report", "project_plan", "email_doc_analysis")


def annotate_support(wf: Workflow, registry: Any) -> None:
    """Mark steps whose (application.operation) has no evidence in the capability registry as unsupported."""
    for st in wf.steps.values():
        if st.operation.startswith("office."):
            st.supported = True  # the office.* verification/planning tools are pure Python
            continue
        app, _, _op = st.operation.partition(".")
        if not _op:
            continue
        ok, why = registry.can(app, st.operation)
        st.supported = ok
        st.support_reason = "" if ok else why
    wf._commit()
