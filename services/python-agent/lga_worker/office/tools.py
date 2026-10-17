"""Tool bindings (contract: tools/office.json) of the pure-Python office modules. Registered by lga_worker/tools/office.py."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.office import filetype, guard, router, verify, workflow
from lga_worker.office.audit import AuditLog, LearningRefused, LearningStore
from lga_worker.office.registry import OfficeCapabilityRegistry
from lga_worker.paths import absolute_path
from lga_worker.registry import Check, ToolContext, ToolOutcome, ToolRegistry

BACKEND = "office-spec"


def _audit(ctx: ToolContext) -> AuditLog:
    return AuditLog(ctx.config.path("paths.logs_dir") / "office")


def _wf_dir(ctx: ToolContext) -> Path:
    return ctx.config.path("paths.state_dir") / "office" / "workflows"


def _prov(ctx: ToolContext, tool: str) -> dict[str, Any]:
    return {"tool": tool, "task_id": ctx.task_id, "step_id": ctx.step_id, "request_id": ctx.request_id}


def audit_tool_call(ctx: ToolContext, tool: str, args: dict[str, Any], status: str, result: Any = None, target: str | None = None) -> None:
    """Best effort audit entry for any office tool; a logging problem never changes the tool's result."""
    try:
        level, _ = guard.classify(tool, args)
        _audit(ctx).record(tool, level=level, status=status, params=args, result=result, provenance=_prov(ctx, tool), target=target)
    except Exception:  # noqa: BLE001
        pass


def _wf(ctx: ToolContext, wid: str) -> workflow.Workflow:
    try:
        return workflow.Workflow.open(_wf_dir(ctx), wid)
    except workflow.WorkflowError as exc:
        raise ToolError(ErrorCode.NOT_FOUND if "no checkpoint" in str(exc) else ErrorCode.INVALID_ARGUMENT, str(exc)) from exc


def register(registry: ToolRegistry) -> None:
    @registry.handler("office.capabilities")
    async def capabilities(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        try:
            snap = await asyncio.to_thread(OfficeCapabilityRegistry().snapshot, args.get("application"), args.get("detail", "summary"), True)
        except KeyError as exc:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, str(exc.args[0])) from exc
        lines = [f"{a['application_id']}: available={a['available']} installed={a['installed']} env={a['execution_environment']} "
                 f"ops={len(a['supported_operations'])} unsupported={len(a['unsupported_operations'])}" for a in snap["applications"]]
        return ToolOutcome(data=snap, backend=BACKEND, observation="Capability registry (registration/importability evidence only, nothing was started; Graph: "
                           f"{snap['graph']['reason']}):\n" + "\n".join(lines))

    @registry.handler("office.detect")
    async def detect(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        info = await asyncio.to_thread(filetype.detect, absolute_path(args["path"]))
        audit_tool_call(ctx, "office.detect", args, info["status"], {"type": info["detected_type"]}, info["path"])
        return ToolOutcome(data=info, backend=BACKEND, observation=f"{info['name']}: real type {info['detected_type']} ({info['label']}), status {info['status']}"
                           + (f", extension mismatch" if info["extension_matches"] is False else "") + (f"; {'; '.join(info['warnings'][:3])}" if info["warnings"] else ""))

    @registry.handler("office.check_action")
    async def check_action(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        res = guard.evaluate(args["operation"], args.get("params") or {}, args.get("context") or {}).as_dict()
        audit_tool_call(ctx, "office.check_action", {"operation": args["operation"], "params": args.get("params")}, res["decision"], {"level": res["level"]})
        return ToolOutcome(data=res, backend=BACKEND, observation=f"{args['operation']}: level {res['level']} ({res['level_name']}) -> {res['decision']}. " + " ".join(res["reasons"]))

    @registry.handler("office.route")
    async def route(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        context = dict(args.get("context") or {})
        if args.get("files"):
            context["files"] = args["files"]
        res = await asyncio.to_thread(router.route, args["request"], None, context)
        q = res["clarification"]
        return ToolOutcome(data=res, backend=BACKEND, observation=(f"Ask first: {q['question']}" if q else f"app={res['primary_app']} op={res['operation_id']} method="
                           f"{(res['method'] or {}).get('selected')}. " + " ".join(res["why"])))

    @registry.handler("office.workflow_plan")
    async def workflow_plan(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        try:
            if args.get("template"):
                steps = workflow.template_steps(args["template"], args.get("params"))
            else:
                steps = [workflow.Step(**s) for s in args.get("steps", [])]
            if not steps:
                raise workflow.WorkflowError("give a template or a non-empty steps list")
            wf = workflow.Workflow(args.get("workflow_id") or workflow.new_workflow_id(), args.get("title") or args.get("template") or "workflow", steps)
            workflow.annotate_support(wf, OfficeCapabilityRegistry())
            path = wf.save(_wf_dir(ctx))
        except (workflow.WorkflowError, TypeError) as exc:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, str(exc)) from exc
        rep = wf.report()
        back = workflow.Workflow.load(path)
        audit_tool_call(ctx, "office.workflow_plan", {"id": wf.id, "steps": len(steps)}, "PENDING", None, str(path))
        return ToolOutcome(data={"workflow_id": wf.id, "checkpoint": str(path), "order": wf.order(), "report": rep, "offline": wf.offline_plan(False)},
                           backend=BACKEND, checks=[Check("checkpoint_readback", len(back.steps) == len(wf.steps), str(path))],
                           observation=f"Workflow {wf.id}: {len(steps)} steps, ready: {rep['ready']}, unsupported: {list(rep['unsupported'])}, needs confirmation: {rep['needs_confirmation']}.")

    @registry.handler("office.workflow_status")
    async def workflow_status(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        if not args.get("workflow_id"):
            items = workflow.list_workflows(_wf_dir(ctx))
            return ToolOutcome(data={"workflows": items}, backend=BACKEND, observation=f"{len(items)} workflow(s)")
        wf = _wf(ctx, args["workflow_id"])
        data = {"report": wf.report(), "resume": wf.resume_plan(), "offline": wf.offline_plan(bool(args.get("online", False)))}
        return ToolOutcome(data=data, backend=BACKEND, observation=f"{wf.id}: {data['report']['overall']}; done {data['report']['completed_steps']}; remaining {data['report']['remaining_steps']}")

    @registry.handler("office.workflow_step")
    async def workflow_step(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        wf = _wf(ctx, args["workflow_id"])
        sid, act = args["step_id"], args["action"]
        extra: dict[str, Any] = {}
        try:
            if act == "start":
                wf.start(sid)
            elif act == "succeed":
                wf.succeed(sid, args.get("evidence"))
            elif act == "fail":
                extra["retry"] = wf.fail(sid, args.get("error", "failed"), args.get("error_kind", ""), args.get("evidence"))
            elif act == "partial":
                wf.partial(sid, args.get("done", []), args.get("remaining", []), args.get("evidence"))
            elif act == "wait_user":
                wf.wait_for_user(sid, args.get("question", "confirm?"))
            elif act == "user_decision":
                wf.user_decision(sid, bool(args.get("approve")))
            elif act == "reconcile":
                extra["reconcile"] = wf.reconcile(sid, args.get("observed"))
            elif act == "retry":
                wf.retry(sid, args.get("error_kind"))
            elif act == "cancel":
                extra["cancel"] = wf.cancel(sid)
            else:
                raise workflow.WorkflowError(f"unknown action {act}")
        except workflow.WorkflowError as exc:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, str(exc), details=exc.detail) from exc
        st = wf.steps[sid]
        import json

        on_disk = {x["id"]: x["status"] for x in json.loads(wf.path.read_text(encoding="utf-8"))["steps"]}  # raw: load() would turn RUNNING into UNKNOWN
        ok = on_disk.get(sid) == st.status
        audit_tool_call(ctx, "office.workflow_step", {"workflow_id": wf.id, "step": sid, "action": act}, st.status)
        return ToolOutcome(data={"step": sid, "status": st.status, "overall": wf.overall(), "report": wf.report(), **extra}, backend=BACKEND,
                           checks=[Check("checkpoint_readback", ok, f"{sid} is {on_disk.get(sid)} on disk")], observation=f"{sid} -> {st.status}; workflow {wf.overall()}")

    @registry.handler("office.verify")
    async def verify_tool(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        opts = dict(args.get("options") or {})
        res = await asyncio.to_thread(verify.verify_file, absolute_path(args["path"]), **opts)
        audit_tool_call(ctx, "office.verify", args, res["verdict"], {"counts": res["counts"]}, res["path"])
        bad = [f"{c['name']}: {c['detail']}" for c in res["checks"] if c["status"] in ("FAIL", "WARN")]
        return ToolOutcome(data=res, backend=BACKEND, observation=f"{Path(res['path']).name}: {res['verdict']} {res['counts']}. " + " | ".join(bad[:6])
                           + (f" Not verified: {'; '.join(res['not_verified'][:3])}" if res["not_verified"] else ""))

    @registry.handler("office.learn")
    async def learn(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        store = LearningStore(ctx.config.path("paths.logs_dir") / "office" / "learning.jsonl")
        try:
            saved = store.store(args["record"])
        except (LearningRefused, TypeError) as exc:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"lesson refused: {exc}") from exc
        stored = any(e.get("timestamp") == saved["timestamp"] for e in store.all())
        return ToolOutcome(data=saved, backend=BACKEND, checks=[Check("stored", stored, str(store.path))], observation=f"Lesson stored ({saved['status']}): {saved['note']}")

    @registry.handler("office.audit_tail")
    async def audit_tail(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        log = _audit(ctx)
        return ToolOutcome(data={"entries": log.tail(int(args.get("limit", 20)), args.get("action")), "chain": log.verify_chain()}, backend=BACKEND,
                           observation="audit log tail")
