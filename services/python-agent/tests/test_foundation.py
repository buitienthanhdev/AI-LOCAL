from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from lga_common.config import Config
from lga_worker.errors import ErrorCode, ToolError
from lga_common.redact import REDACTED, Redactor
from lga_worker.shell.powershell import looks_like_elevation_failure, resolve_powershell, run_powershell
from lga_worker.sta import StaExecutor
from lga_worker.windows.elements import ElementCache, matches_query, normalize_control_type
from lga_worker.windows.provider import ElementLocator, ElementQuery


def test_config_defaults_load(config: Config) -> None:
    assert config.get("agent.mode") in {"normal", "trusted", "full_access"}
    assert config.seconds("desktop.settle_ms") > 0
    assert config.path("paths.artifacts_dir").is_absolute()


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("Cookie: __Secure-1PSID=abcdef123456; NID=zzz", "abcdef123456"),
        ("__Secure-1PSIDTS=sidts-XYZ987654", "sidts-XYZ987654"),
        ("Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
        ('{"api_key": "k-1234567890"}', "k-1234567890"),
        ("token sk-abcdefghijklmnopqrstuv end", "sk-abcdefghijklmnopqrstuv"),
        ("  API key:     p5w8IV8s8rWF...VhPE  (locked by flag/env)", "p5w8IV8s8rWF"),
    ],
)
def test_redactor_removes_secrets(raw: str, secret: str) -> None:
    out = Redactor()(raw)
    assert secret not in out
    assert REDACTED in out


def test_redactor_literal() -> None:
    redactor = Redactor()
    redactor.add_literal("super-secret-value")
    assert "super-secret-value" not in redactor("x super-secret-value y")



def test_powershell_separates_streams_and_exit_code() -> None:
    shell = resolve_powershell("pwsh")
    result = run_powershell(
        "Write-Output 'xin chào'; [Console]::Error.WriteLine('oops'); exit 3",
        executable=shell, cwd=None, timeout_s=60, max_output_bytes=10_000,
    )
    assert result.exit_code == 3
    assert "xin chào" in result.stdout
    assert "oops" in result.stderr
    assert not result.timed_out


def test_powershell_timeout_kills_process_tree() -> None:
    shell = resolve_powershell("pwsh")
    result = run_powershell(
        "Start-Process -NoNewWindow -FilePath ping -ArgumentList '-n','30','127.0.0.1'; Start-Sleep 30",
        executable=shell, cwd=None, timeout_s=3, max_output_bytes=10_000,
    )
    assert result.timed_out
    assert result.duration_ms < 15_000


def test_powershell_cancel_event() -> None:
    shell = resolve_powershell("pwsh")
    cancel = threading.Event()
    timer = threading.Timer(1.5, cancel.set)
    timer.start()
    result = run_powershell("Start-Sleep 30", executable=shell, cwd=None, timeout_s=60,
                            max_output_bytes=1000, cancel_event=cancel)
    assert "cancelled" in result.stderr
    assert result.duration_ms < 15_000


def test_powershell_output_cap() -> None:
    shell = resolve_powershell("pwsh")
    result = run_powershell("'x' * 50000", executable=shell, cwd=None, timeout_s=60, max_output_bytes=1000)
    assert result.truncated and len(result.stdout) <= 1000


def test_elevation_detection() -> None:
    from lga_worker.windows.provider import ShellResult

    assert looks_like_elevation_failure(ShellResult(740, "", "", False, False, 1))
    assert looks_like_elevation_failure(ShellResult(1, "", "Access is denied.", False, False, 1))
    assert not looks_like_elevation_failure(ShellResult(0, "", "", False, False, 1))


def test_element_ids_differ_between_worker_processes() -> None:
    loc = ElementLocator(1, "OK", "ButtonControl", "", "")
    first = ElementCache(max_entries=10, prefix="ab").register(object(), (1,), loc)
    second = ElementCache(max_entries=10, prefix="cd").register(object(), (1,), loc)
    assert first == "eab1" and second == "ecd1"


def test_element_cache_reuses_ids_and_evicts() -> None:
    cache = ElementCache(max_entries=2)
    loc = ElementLocator(1, "OK", "ButtonControl", "", "")
    first = cache.register(object(), (1, 2), loc)
    assert cache.register(object(), (1, 2), loc) == first
    cache.register(object(), (3,), loc)
    cache.register(object(), (4,), loc)
    assert cache.get(first) is None


def test_query_matching() -> None:
    assert normalize_control_type("ButtonControl") == normalize_control_type("button")
    query = ElementQuery(name_contains="save", control_type="Button")
    assert matches_query(query, name="Save As", control_type="ButtonControl", automation_id="", class_name="")
    assert not matches_query(query, name="Open", control_type="ButtonControl", automation_id="", class_name="")


def test_sta_executor_runs_on_single_thread_and_times_out() -> None:
    executor = StaExecutor("test")

    async def scenario() -> None:
        ids = {await executor.run(5, threading.get_ident) for _ in range(3)}
        assert len(ids) == 1
        with pytest.raises(ToolError) as exc:
            await executor.run(0.2, __import__("time").sleep, 2)
        assert exc.value.code == ErrorCode.TIMEOUT
        # A fresh thread serves calls after the stuck one was abandoned.
        assert await executor.run(5, lambda: 42) == 42

    asyncio.run(scenario())
    executor.shutdown()


def test_powershell_output_has_no_ansi_escapes() -> None:
    shell = resolve_powershell("pwsh")
    result = run_powershell("$PSVersionTable.PSVersion", executable=shell, cwd=None, timeout_s=60, max_output_bytes=10_000)
    assert "[" not in result.stdout and "Major" in result.stdout


def test_upstream_shim_can_load_twice_in_one_process() -> None:
    from lga_worker.windows import _upstream

    first = _upstream.load()
    second = _upstream.load()
    assert first[0] is second[0]
