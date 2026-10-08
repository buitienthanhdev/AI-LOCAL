from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from lga_common.config import load_config
from lga_ui.tasks import TaskClient


@pytest.fixture
def client(tmp_path: Path) -> TaskClient:
    real = load_config()
    root = tmp_path / "repo"
    (root / "configs").mkdir(parents=True)
    shutil.copy(real.root / "configs" / "config.example.toml", root / "configs" / "config.example.toml")
    config = load_config(root)
    fake_agent = tmp_path / "agent.exe"
    fake_agent.write_bytes(b"")
    return TaskClient(config=config, agent_exe=fake_agent)


def test_invalid_task_id_rejected(client: TaskClient) -> None:
    with pytest.raises(ValueError):
        client.record("../escape")
    with pytest.raises(ValueError):
        client.control("bad id", "cancel")


def test_record_and_pending_request(client: TaskClient) -> None:
    tasks = client.tasks_dir
    tasks.mkdir(parents=True)
    tid = "20261008-000000-abcdef"
    (tasks / f"{tid}.json").write_text(json.dumps({"task_id": tid, "status": "executing"}))
    assert client.record(tid)["status"] == "executing"
    assert client.record("20261008-000000-nope") is None
    (tasks / f"{tid}.request.json").write_text(json.dumps({"id": "r1", "kind": "confirm"}))
    assert client.pending_request(tid)["id"] == "r1"


def test_respond_writes_matching_id(client: TaskClient) -> None:
    tasks = client.tasks_dir
    tasks.mkdir(parents=True)
    tid = "20261008-000000-abcdef"
    (tasks / f"{tid}.request.json").write_text(json.dumps({"id": "r9", "kind": "confirm"}))
    assert client.respond(tid, approve=True)
    assert json.loads((tasks / f"{tid}.response.json").read_text()) == {"id": "r9", "approve": True}
    # No pending request -> nothing to answer.
    (tasks / f"{tid}.request.json").unlink()
    assert client.respond(tid, approve=False) is False


def test_events_paging(client: TaskClient) -> None:
    logs = client.config.path("paths.logs_dir") / "tasks"
    logs.mkdir(parents=True)
    tid = "20261008-000000-abcdef"
    (logs / f"{tid}.jsonl").write_text("\n".join(json.dumps({"kind": "status", "n": i}) for i in range(5)) + "\n")
    events, cursor = client.events(tid, 0)
    assert len(events) == 5 and cursor == 5
    assert client.events(tid, 5) == ([], 5)


def test_invalid_mode_rejected(client: TaskClient) -> None:
    with pytest.raises(ValueError):
        client.start("hello", "root")
