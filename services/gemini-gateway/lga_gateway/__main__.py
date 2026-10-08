"""Run the gateway: `python -m lga_gateway` (normally started by the agent core or scripts/start-gateway.ps1).

Stops on Ctrl+C or `POST /v1/admin/shutdown`; managed backends are stopped with it.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiohttp import web

from lga_common.config import ConfigError, load_config
from lga_common.logs import setup_logging
from lga_common.redact import Redactor

from lga_gateway.app import GatewayAssembly, assemble
from lga_gateway.server import STOP_KEY, build_app

logger = logging.getLogger(__name__)


async def serve(assembly: GatewayAssembly) -> None:
    app = build_app(assembly.gateway, assembly.api_key, assembly.client_max_size)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, assembly.host, assembly.port)
        await site.start()
        logger.info("gateway listening on http://%s:%d", assembly.host, assembly.port)
        await app[STOP_KEY].wait()
        logger.info("shutdown requested")
    finally:
        await runner.cleanup()


def main() -> None:
    try:
        config = load_config()
        redactor = Redactor(config.get("observability.redact_env"))
        setup_logging("gateway", config.path("paths.logs_dir"), str(config.get("observability.log_level")), redactor)
        assembly = assemble(config, redactor)
    except ConfigError as exc:
        print(f"gateway configuration error: {exc}", file=sys.stderr)
        sys.exit(2)
    try:
        asyncio.run(serve(assembly))
    except KeyboardInterrupt:
        logger.info("interrupted")


if __name__ == "__main__":
    main()
