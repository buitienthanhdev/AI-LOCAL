from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from lga_common.config import load_config
from lga_ui.server import build_app

TOKEN = "test-token-123456"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    real = load_config()
    root = tmp_path / "repo"
    (root / "configs").mkdir(parents=True)
    shutil.copy(real.root / "configs" / "config.example.toml", root / "configs" / "config.example.toml")
    return root


@pytest.fixture
async def client(aiohttp_client, workspace: Path):
    config = load_config(workspace)
    app = build_app(config, Path(sys.executable), "127.0.0.1", 8791, token=TOKEN)
    return await aiohttp_client(app)


def headers(**extra: str) -> dict[str, str]:
    return {"X-LGA-Token": TOKEN, **extra}


async def test_index_embeds_token_and_api_requires_it(client) -> None:
    page = await client.get("/")
    assert page.status == 200 and TOKEN in await page.text()
    assert (await client.get("/api/tasks")).status == 401
    assert (await client.get("/api/tasks", headers={"X-LGA-Token": "wrong"})).status == 401
    assert (await client.get("/api/tasks", headers=headers())).status == 200


async def test_cross_origin_requests_are_refused(client) -> None:
    response = await client.post("/api/tasks", headers=headers(Origin="https://evil.example"), json={"task": "x"})
    assert response.status == 403


async def test_artifact_path_must_stay_inside_artifacts(client, workspace: Path) -> None:
    secret = workspace / ".env"
    secret.write_text("LGA_GATEWAY_API_KEY=x")
    response = await client.get("/api/artifact", params={"path": str(secret)}, headers=headers())
    assert response.status == 404
    shots = workspace / "artifacts" / "screenshots" / "t1"
    shots.mkdir(parents=True)
    (shots / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    ok = await client.get("/api/artifact", params={"path": str(shots / "a.png")}, headers=headers())
    assert ok.status == 200


async def test_task_detail_events_and_confirmation_round_trip(client, workspace: Path) -> None:
    tasks = workspace / "memory" / "tasks"
    tasks.mkdir(parents=True)
    task_id = "20261008-000000-abcdef"
    (tasks / f"{task_id}.json").write_text(json.dumps({"task_id": task_id, "goal": "g", "status": "executing",
                                                       "mode": "normal", "steps": [], "updated_at": "x"}))
    (tasks / f"{task_id}.request.json").write_text(json.dumps({"id": "req1", "kind": "confirm", "tool": "shell.exec"}))
    logs = workspace / "logs" / "tasks"
    logs.mkdir(parents=True)
    (logs / f"{task_id}.jsonl").write_text("\n".join(json.dumps({"kind": "status", "n": i}) for i in range(3)) + "\n")

    detail = await (await client.get(f"/api/tasks/{task_id}", headers=headers())).json()
    assert detail["pending_request"]["id"] == "req1"
    first = await (await client.get(f"/api/tasks/{task_id}/events?after=0", headers=headers())).json()
    assert len(first["events"]) == 3 and first["next"] == 3
    again = await (await client.get(f"/api/tasks/{task_id}/events?after=3", headers=headers())).json()
    assert again["events"] == []

    response = await client.post(f"/api/tasks/{task_id}/respond", headers=headers(), json={"approve": True})
    assert response.status == 200
    answer = json.loads((tasks / f"{task_id}.response.json").read_text())
    assert answer == {"id": "req1", "approve": True}


async def test_invalid_task_ids_and_modes(client) -> None:
    assert (await client.get("/api/tasks/..%2F..%2Fsecret", headers=headers())).status in (400, 404)
    bad_mode = await client.post("/api/tasks", headers=headers(), json={"task": "x", "mode": "root"})
    assert bad_mode.status == 400
