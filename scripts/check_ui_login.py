#!/usr/bin/env python3
"""Перевірка входу через справжній браузер.

НАВІЩО ОКРЕМА ПЕРЕВІРКА. Набір автоматичних перевірок звертається до
програмного інтерфейсу напряму й нічого не знає про те, що відбувається в
браузері. Через це всі 181 перевірка проходили, тоді як вхід у систему був
несправний: маркер доступу зберігався, програмний інтерфейс відповідав 200,
а застосунок лишався на сторінці входу, бо стан входу обчислювався поза
станом React і ніщо не викликало перемальовування. Ззовні це виглядало як
«пароль не працює».

Перевірка відкриває сторінку, заповнює поля, натискає кнопку й переконується,
що застосунок справді опинився на дашборді, а не повернувся на вхід.

    python scripts/check_ui_login.py --url http://127.0.0.1:8000 \
        --email devops@example.com --password devops123

Пароль можна передати змінною середовища UI_PASSWORD, щоб він не потрапив
в історію команд. Код повернення 0 — вхід працює, 1 — ні.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

CDP_PORT = 9321

# Поля React не реагують на пряме присвоєння value: потрібен рідний сетер
# прототипу й подія input, інакше стан компонента лишається порожнім.
FILL = """(function (sel, val) {
  const el = document.querySelector(sel);
  if (!el) return 'немає поля ' + sel;
  const set = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  set.call(el, val);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  return 'ok';
})"""

STATE = """JSON.stringify({
  hash: location.hash,
  token: !!localStorage.getItem('cicd_token'),
  error: (document.querySelector('.error') || {}).innerText || null,
  shell: !!document.querySelector('.sidebar, nav a[href*="dashboard"]')
})"""


def wait_port(port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise SystemExit(f"порт {port} не піднявся за {timeout} с")


async def run(url: str, email: str, password: str, timeout: float) -> int:
    import websockets

    with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=10) as r:
        page = next(t for t in json.loads(r.read()) if t["type"] == "page")

    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
        n = 0

        async def call(method: str, **params):
            nonlocal n
            n += 1
            await ws.send(json.dumps({"id": n, "method": method, "params": params}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == n:
                    if "error" in msg:
                        raise RuntimeError(f"{method}: {msg['error']}")
                    return msg.get("result", {})

        await call("Page.enable")
        await call("Runtime.enable")
        await call("Emulation.setDeviceMetricsOverride",
                   width=1280, height=900, deviceScaleFactor=1, mobile=False)
        # Сховище очищається, інакше перевірка мовчки проходить на вже
        # збереженому маркері й нічого не перевіряє.
        await call("Page.navigate", url=f"{url}/#/login")
        await asyncio.sleep(2)
        await call("Runtime.evaluate", expression="localStorage.clear()")
        await call("Page.reload")
        await asyncio.sleep(2.5)

        for sel, val in (("#email", email), ("#password", password)):
            r = await call("Runtime.evaluate", returnByValue=True,
                           expression=f"({FILL})({json.dumps(sel)}, {json.dumps(val)})")
            if r["result"].get("value") != "ok":
                print(f"не вдалося заповнити {sel}: {r['result'].get('value')}")
                return 1

        await call("Runtime.evaluate",
                   expression="document.querySelector('form button[type=submit]').click()")

        deadline = time.time() + timeout
        state = {}
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            r = await call("Runtime.evaluate", returnByValue=True, expression=STATE)
            state = json.loads(r["result"]["value"])
            if state.get("error"):
                print(f"сторінка показала помилку: {state['error']}")
                return 1
            if state["hash"].startswith("#/dashboard") and state["shell"]:
                print(f"вхід працює: {state['hash']}, маркер збережено, інтерфейс відкрито")
                return 0

        print("вхід НЕ завершився: " + json.dumps(state, ensure_ascii=False))
        print("  маркер збережено, але застосунок лишився на сторінці входу —"
              " саме так виглядає втрата стану входу в App")
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Перевірка входу через браузер")
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--email", default="devops@example.com")
    ap.add_argument("--password", default=os.getenv("UI_PASSWORD", "devops123"))
    ap.add_argument("--timeout", type=float, default=15.0)
    args = ap.parse_args()

    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        print("не знайдено google-chrome — перевірку пропущено")
        return 0

    profile = tempfile.mkdtemp(prefix="ui-login-check-")
    browser = subprocess.Popen(
        [chrome, "--headless=new", f"--remote-debugging-port={CDP_PORT}",
         f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
         "--hide-scrollbars", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_port(CDP_PORT)
        return asyncio.run(run(args.url.rstrip("/"), args.email, args.password, args.timeout))
    finally:
        browser.send_signal(signal.SIGTERM)
        try:
            browser.wait(timeout=10)
        except subprocess.TimeoutExpired:
            browser.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
