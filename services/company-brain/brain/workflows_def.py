"""Định nghĩa các workflow bền của hệ thống. Mỗi bước idempotent; trạng thái lưu sau mỗi bước (xem workflow.py)."""
from . import workflow
from .catalog import connect


# ---------------------------------------------------------------- MAINTENANCE (mỗi giờ)
def _contrib_backfill(state):
    from . import contrib
    conn = connect()
    n = contrib.backfill(conn)
    conn.commit()
    conn.close()
    return {"contrib_backfilled": n}


def _git_sync(state):
    from . import gitsync
    return {"git": gitsync.sync_all()}


def _graph_rebuild(state):
    from . import graph
    conn = connect()
    r = graph.rebuild(conn)
    conn.close()
    return {"graph": r}


def _gaps(state):
    from . import gaps
    conn = connect()
    r = gaps.run(conn)
    conn.close()
    return {"gaps": r}


def _canary_eval(state):
    from . import registry
    return {"canary": {role: registry.evaluate_canary(role) for role in ("chat",)}}


def _prune(state):
    from . import obs, watcher
    obs.prune()
    watcher.prune()
    return {}


def _backup(state):
    from . import backup
    b = backup.run_backup()  # tự bỏ qua nếu đã sao lưu trong 20 giờ
    st = backup.status().get("integrity") or {}
    i = backup.run_integrity() if not st or __import__("time").time() - st.get("ts", 0) > 24 * 3600 else st
    return {"backup": b, "integrity": i}


workflow.define("MAINTENANCE", [("contrib_backfill", _contrib_backfill), ("git_sync", _git_sync),
                                ("graph_rebuild", _graph_rebuild), ("knowledge_gaps", _gaps),
                                ("canary_evaluation", _canary_eval), ("prune", _prune), ("backup", _backup)])


# ---------------------------------------------------------------- DEPLOY_MODEL
def _dep_register(state):
    from . import registry
    registry.register(state["model"], state.get("role", "chat"), state.get("base_model"), state.get("quant"),
                      state.get("parent"), state.get("dataset_version"), state.get("notes"))
    return {}


def _dep_benchmark(state):
    from . import bench
    r = bench.run_suite(state["model"], suites=state.get("suites") or ["core"], quick=state.get("quick", False))
    return {"bench_score": r["score"], "bench_run": r["run_id"]}


def _dep_canary(state):
    from . import registry
    r = registry.start_canary(state["model"], state.get("canary_percent", 10), state.get("actor", "system"))
    if not r["ok"]:
        return {"rejected": r["error"]}  # bị từ chối (vd. regression) — workflow kết thúc, KHÔNG deploy
    return {"canary_started": True}


workflow.define("DEPLOY_MODEL", [("register", _dep_register), ("benchmark", _dep_benchmark), ("canary", _dep_canary)])


# ---------------------------------------------------------------- BUILD_TRAINING_DATASET
def _ds_build(state):
    from . import learning
    return {"dataset": learning.build_dataset()}


workflow.define("BUILD_TRAINING_DATASET", [("build", _ds_build)])
