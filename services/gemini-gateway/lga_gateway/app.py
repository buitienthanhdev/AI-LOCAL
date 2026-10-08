"""Builds a `GeminiGateway` from configuration."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlparse

from lga_common.config import Config, ConfigError, resolve_secret
from lga_common.redact import Redactor

from lga_gateway.attachments import AttachmentStore
from lga_gateway.backends.base import Backend
from lga_gateway.backends.web2api_go import Web2ApiGoBackend
from lga_gateway.backends.webai_to_api import WebAiToApiBackend
from lga_gateway.gateway import GeminiGateway, Route
from lga_gateway.resilience import BackoffPolicy, BreakerSettings, CircuitBreaker
from lga_gateway.supervisor import ManagedProcess, ProcessSpec, create_kill_on_close_job

# Request bodies carry base64 images (4/3 expansion) plus prompt text.
BODY_OVERHEAD_BYTES = 4_000_000


@dataclass
class GatewayAssembly:
    gateway: GeminiGateway
    api_key: str
    host: str
    port: int
    client_max_size: int
    redactor: Redactor


def _require_loopback(host: str, what: str) -> None:
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ConfigError(f"{what} must bind/connect to a loopback address, got {host!r}")


def _port(url: str) -> str:
    parsed = urlparse(url)
    if parsed.port is None:
        raise ConfigError(f"{url} must include an explicit port")
    _require_loopback(parsed.hostname or "", url)
    return str(parsed.port)


def _primary(config: Config, attachments: AttachmentStore, redactor: Redactor, job: int,
             connect_timeout_s: float) -> tuple[Backend, ManagedProcess | None, float]:
    section = config.section("gateway.primary")
    api_key = resolve_secret(config, section["api_key_secret"])
    admin_token = resolve_secret(config, section["admin_token_secret"])
    redactor.add_literal(api_key)
    redactor.add_literal(admin_token)
    backend = Web2ApiGoBackend("primary", section["base_url"], section["default_model"], attachments,
                               connect_timeout_s, api_key, admin_token)
    process = None
    if section["manage_process"]:
        if not api_key or not admin_token:
            raise ConfigError("primary backend secrets are missing; run scripts/setup-gateway.ps1")
        work_dir = config.path("gateway.primary.work_dir")
        env = {"API_KEY": api_key, "ADMIN_TOKEN": admin_token}
        process = ManagedProcess(ProcessSpec(
            name="primary",
            argv=[str(config.path("gateway.primary.binary")), "--config", str(work_dir / "config.json"),
                  "--port", _port(section["base_url"])],
            cwd=work_dir, env=env, log_path=config.path("paths.logs_dir") / "gateway" / "primary.log",
        ), job, redactor)
    return backend, process, config.seconds("gateway.primary.startup_timeout_ms")


def _fallback(config: Config, attachments: AttachmentStore, redactor: Redactor, job: int,
              connect_timeout_s: float) -> tuple[Backend, ManagedProcess | None, float]:
    section = config.section("gateway.fallback")
    backend = WebAiToApiBackend("fallback", section["base_url"], section["default_model"], attachments,
                                connect_timeout_s)
    process = None
    if section["manage_process"]:
        work_dir = config.path("gateway.fallback.work_dir")
        source = config.path("gateway.fallback.source_dir")
        runtime_dir = work_dir / "runtime"
        process = ManagedProcess(ProcessSpec(
            name="fallback",
            argv=[str(config.path("gateway.fallback.python")), str(source / "src" / "run.py"),
                  "--host", "127.0.0.1", "--port", _port(section["base_url"]), "--disable-access-logs"],
            cwd=work_dir,
            env={"RUNTIME_DIR": str(runtime_dir), "AUTH_STATE_DIR": str(runtime_dir / "auth"),
                 "CONVERSATION_SNAPSHOT_DB": str(runtime_dir / "conversations.db"),
                 "PYTHONIOENCODING": "utf-8"},
            log_path=config.path("paths.logs_dir") / "gateway" / "fallback.log",
        ), job, redactor)
    return backend, process, config.seconds("gateway.fallback.startup_timeout_ms")


def assemble(config: Config, redactor: Redactor) -> GatewayAssembly:
    gw = config.section("gateway")
    _require_loopback(gw["listen_host"], "gateway.listen_host")
    api_key = resolve_secret(config, gw["api_key_secret"])
    if not api_key:
        raise ConfigError(f"secret {gw['api_key_secret']} is not configured; run scripts/setup-gateway.ps1")
    redactor.add_literal(api_key)

    attachments = AttachmentStore(int(gw["attachment_max_bytes"]), int(gw["attachment_total_bytes"]),
                                  config.seconds("gateway.attachment_ttl_ms"))
    breaker_cfg = config.section("gateway.circuit_breaker")
    breaker_settings = BreakerSettings(int(breaker_cfg["failure_threshold"]),
                                       config.seconds("gateway.circuit_breaker.open_duration_ms"),
                                       config.seconds("gateway.circuit_breaker.max_open_duration_ms"))
    retry_cfg = config.section("gateway.retry")
    retry = BackoffPolicy(int(retry_cfg["max_attempts"]), config.seconds("gateway.retry.initial_backoff_ms"),
                          config.seconds("gateway.retry.max_backoff_ms"), float(retry_cfg["multiplier"]),
                          float(retry_cfg["jitter"]))
    connect_timeout_s = config.seconds("gateway.connect_timeout_ms")
    job = create_kill_on_close_job()
    routes = []
    for build in (_primary, _fallback):
        backend, process, startup_timeout = build(config, attachments, redactor, job, connect_timeout_s)
        routes.append(Route(backend, CircuitBreaker(breaker_settings), process, startup_timeout))
    gateway = GeminiGateway(routes, attachments, retry, config.seconds("gateway.request_timeout_ms"),
                            config.seconds("gateway.health_interval_ms"))
    return GatewayAssembly(gateway=gateway, api_key=api_key, host=gw["listen_host"], port=int(gw["listen_port"]),
                           client_max_size=int(gw["attachment_max_bytes"]) * 2 + BODY_OVERHEAD_BYTES,
                           redactor=redactor)
