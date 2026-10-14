"""A local stand-in for the Gemini page, built from what the supervised exchange of phase F observed.

Same structure the shipped selectors rely on (custom elements, role=textbox contenteditable, a send button that
exists only while the composer has text, `.send-button.stop` and `aria-busy` while generating, one
`model-response > message-content` per answer, send button removed when done). It is NOT Gemini: it proves the
adapter/driver/gateway chain against that structure and counts, on the server side, how many prompts really
arrived, which is the ground truth for "never sent twice".

The answer is "ECHO: <prompt>" so a mixed-up conversation is visible. Behaviour is switched at runtime:
    delay_ms   generation time          stuck      never finish generating (answer stays partial)
    nostop     no generating signal     login      render signed out (no account link)
    banner     show an error banner after sending
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

from aiohttp import web

PAGE = r"""<!doctype html><html lang="vi"><head><meta charset="utf-8"><title>Fake Gemini</title></head><body>
<div id="account"></div>
<bard-mode-switcher><button id="mode">Gemini<br>Pro</button></bard-mode-switcher>
<div id="banner"></div>
<div id="conv"></div>
<input-area-v2>
  <rich-textarea><div role="textbox" contenteditable="true" id="box" style="min-height:24px;border:1px solid #888"></div></rich-textarea>
  <div id="sendwrap"></div>
</input-area-v2>
<script>
(async () => {
  const cfg = await (await fetch('/__config')).json();
  if (!cfg.login) {
    document.getElementById('account').innerHTML = '<a href="https://accounts.google.com/SignOutOptions?x=1">Account</a>';
  }
  const box = document.getElementById('box'), wrap = document.getElementById('sendwrap'), conv = document.getElementById('conv');
  const sync = () => {
    const has = box.innerText.trim().length > 0;
    const present = !!wrap.querySelector('.send-button');
    if (has && !present) wrap.innerHTML = '<gem-icon-button class="send-button"><button aria-label="Send"></button></gem-icon-button>';
    if (!has && present && !wrap.dataset.busy) wrap.innerHTML = '';
  };
  box.addEventListener('input', sync);
  new MutationObserver(sync).observe(box, {childList: true, characterData: true, subtree: true});
  wrap.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('.send-button button');
    if (!btn || wrap.dataset.busy) return;
    const prompt = box.innerText.trim();
    if (!prompt) return;
    box.innerText = '';
    wrap.dataset.busy = '1';
    wrap.querySelector('.send-button').classList.add('stop');
    const q = document.createElement('user-query'); q.textContent = 'You said ' + prompt; conv.appendChild(q);
    await fetch('/__submit', {method: 'POST', body: JSON.stringify({prompt})});
    const cur = await (await fetch('/__config')).json();
    if (cur.banner) document.getElementById('banner').innerHTML = '<div class="error-banner">Something went wrong</div>';
    const answer = 'ECHO: ' + prompt;
    const r = document.createElement('model-response');
    const mc = document.createElement('message-content');
    const md = document.createElement('div'); md.className = 'markdown'; md.setAttribute('aria-busy', 'true');
    mc.appendChild(md); r.appendChild(document.createTextNode('Gemini said ')); r.appendChild(mc);
    await new Promise(res => setTimeout(res, 150));
    conv.appendChild(r);
    const words = answer.split(' ');
    const step = Math.max(10, (cur.delay_ms || 300) / Math.max(1, words.length));
    for (let i = 1; i <= words.length; i++) {
      md.textContent = words.slice(0, i).join(' ');
      if (cur.stuck && i >= Math.min(2, words.length)) { await new Promise(() => {}); }
      await new Promise(res => setTimeout(res, step));
    }
    if (cur.nostop) { /* no "generating" signal at all: the stop class and busy flag are never shown */ }
    if (!cur.nostop) md.setAttribute('aria-busy', 'false');
    delete wrap.dataset.busy;
    wrap.innerHTML = '';
  });
  // a generating signal that the "nostop" mode withholds
  new MutationObserver(async () => {
    const cur = await (await fetch('/__config')).json();
    if (cur.nostop) {
      document.querySelectorAll('.send-button.stop').forEach(e => e.classList.remove('stop'));
      document.querySelectorAll('[aria-busy]').forEach(e => e.removeAttribute('aria-busy'));
    }
  }).observe(document.body, {subtree: true, attributes: true, childList: true});
})();
</script></body></html>"""


class FakeGemini:
    """aiohttp app run on its own thread/loop so a synchronous test driver can use it."""

    def __init__(self) -> None:
        self.config: dict[str, Any] = {"delay_ms": 300, "stuck": False, "nostop": False, "login": False, "banner": False}
        self.submissions: list[dict[str, Any]] = []
        self.page_loads = 0
        self.port = 0
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner: web.AppRunner | None = None
        self._ready = threading.Event()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/app"

    def set(self, **changes: Any) -> None:
        with self._lock:
            self.config.update(changes)

    def prompts(self) -> list[str]:
        with self._lock:
            return [s["prompt"] for s in self.submissions]

    async def _app(self, request: web.Request) -> web.Response:
        with self._lock:
            self.page_loads += 1
        return web.Response(text=PAGE, content_type="text/html")

    async def _config(self, request: web.Request) -> web.Response:
        with self._lock:
            return web.json_response(dict(self.config))

    async def _submit(self, request: web.Request) -> web.Response:
        body = json.loads(await request.text())
        with self._lock:
            self.submissions.append({"prompt": body["prompt"], "at": time.time()})
            count = len(self.submissions)
        return web.json_response({"count": count})

    def start(self) -> "FakeGemini":
        def run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            app = web.Application()
            app.add_routes([web.get("/app", self._app), web.get("/__config", self._config), web.post("/__submit", self._submit)])
            runner = web.AppRunner(app, access_log=None)
            self._runner = runner
            loop.run_until_complete(runner.setup())
            site = web.TCPSite(runner, "127.0.0.1", 0)
            loop.run_until_complete(site.start())
            self.port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
            self._ready.set()
            loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        assert self._ready.wait(10), "fake Gemini did not start"
        return self

    def stop(self) -> None:
        loop, runner = self._loop, self._runner
        if loop and runner:
            asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(10)
            loop.call_soon_threadsafe(loop.stop)
