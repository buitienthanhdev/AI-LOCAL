"""Run the UI: `python -m lga_ui` (or scripts/start-ui.ps1), then open the printed URL."""

from __future__ import annotations

import ipaddress
import sys

from aiohttp import web

from lga_common.config import ConfigError, load_config

from lga_ui.server import build_app, find_agent_exe


def main() -> None:
    try:
        config = load_config()
        host = str(config.get("ui.listen_host"))
        port = int(config.get("ui.listen_port"))
        if not (host == "localhost" or ipaddress.ip_address(host).is_loopback):
            raise ConfigError(f"ui.listen_host must be a loopback address, got {host!r}")
        agent = find_agent_exe(config.root)
    except (ConfigError, FileNotFoundError, ValueError) as exc:
        print(f"agent-ui: {exc}", file=sys.stderr)
        sys.exit(2)
    print(f"Local Gemini Agent UI: http://{host}:{port}/  (agent: {agent})", flush=True)
    web.run_app(build_app(config, agent, host, port), host=host, port=port, print=None, access_log=None)


if __name__ == "__main__":
    main()
