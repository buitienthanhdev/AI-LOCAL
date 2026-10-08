"""Desktop chat window for the unified agent (pywebview / WebView2), like Company Brain's panel.

It hosts the local web UI (`server.build_app`) on a loopback port in a background thread and opens a native
WebView2 window pointing at it. The page is the full chat interface (task entry, live steps, confirmations,
screenshots, verification). This is the single chat for the whole agent.

Run: python -m lga_ui.desktop   (or scripts/start-widget.ps1)
Falls back to opening the system browser if WebView2 is unavailable.
"""

from __future__ import annotations

import asyncio
import ipaddress
import secrets
import socket
import sys
import threading
import time
import webbrowser

from aiohttp import web

from lga_common.config import ConfigError, load_config

from lga_ui.server import build_app, find_agent_exe

WINDOW_TITLE = "Local Gemini Agent"
WINDOW_WIDTH = 940
WINDOW_HEIGHT = 760
SERVER_READY_TIMEOUT_S = 15.0


def _wait_until_listening(host: str, port: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            if probe.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.1)
    return False


def _serve_in_background(app: web.Application, host: str, port: int) -> threading.Thread:
    def run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        runner = web.AppRunner(app, access_log=None)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, host, port)
        loop.run_until_complete(site.start())
        loop.run_forever()

    thread = threading.Thread(target=run, name="lga-ui-server", daemon=True)
    thread.start()
    return thread


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

    token = secrets.token_urlsafe(32)
    app = build_app(config, agent, host, port, token=token)
    _serve_in_background(app, host, port)
    url = f"http://{host}:{port}/"
    if not _wait_until_listening(host, port, SERVER_READY_TIMEOUT_S):
        print(f"agent-ui: server did not start on {url}", file=sys.stderr)
        sys.exit(1)

    try:
        import webview
    except ImportError:
        print(f"pywebview not installed; opening {url} in your browser instead.", file=sys.stderr)
        webbrowser.open(url)
        # Keep the server alive while the browser tab is open.
        threading.Event().wait()
        return

    webview.create_window(WINDOW_TITLE, url, width=WINDOW_WIDTH, height=WINDOW_HEIGHT,
                          min_size=(380, 460), text_select=True)
    # gui='edgechromium' is the Windows default (WebView2). Falls back to the browser on failure.
    try:
        webview.start()
    except Exception as exc:  # noqa: BLE001 - last-resort fallback so the user still gets a UI
        print(f"WebView2 window failed ({exc}); opening {url} in your browser.", file=sys.stderr)
        webbrowser.open(url)
        threading.Event().wait()


if __name__ == "__main__":
    main()
