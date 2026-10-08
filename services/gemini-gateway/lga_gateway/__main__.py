"""Run the gateway: `python -m lga_gateway` (normally started by the agent core or scripts/start-gateway.ps1)."""

from __future__ import annotations

import logging
import sys

from aiohttp import web

from lga_common.config import ConfigError, load_config
from lga_common.logs import setup_logging
from lga_common.redact import Redactor

from lga_gateway.app import assemble
from lga_gateway.server import build_app


def main() -> None:
    try:
        config = load_config()
        redactor = Redactor(config.get("observability.redact_env"))
        setup_logging("gateway", config.path("paths.logs_dir"), str(config.get("observability.log_level")), redactor)
        assembly = assemble(config, redactor)
    except ConfigError as exc:
        print(f"gateway configuration error: {exc}", file=sys.stderr)
        sys.exit(2)
    logging.getLogger(__name__).info("gateway listening on http://%s:%d", assembly.host, assembly.port)
    app = build_app(assembly.gateway, assembly.api_key, assembly.client_max_size)
    web.run_app(app, host=assembly.host, port=assembly.port, access_log=None, print=None, handle_signals=True)


if __name__ == "__main__":
    main()
