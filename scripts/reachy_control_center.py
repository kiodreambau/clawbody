#!/usr/bin/env python3
"""Local button UI for Reachy Mini apps.

This intentionally uses only the Python standard library so it can run from a
double-clickable desktop launcher without project setup.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


HOST = "127.0.0.1"
PORT = 7862
DAEMON = "http://127.0.0.1:8000"
CLAWBODY_UI = "http://127.0.0.1:7861"
OPENCLAW_UI = "http://127.0.0.1:18789"
HAND_TRACKER_UI = "http://127.0.0.1:8042"


def request_json(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{DAEMON}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            raw = response.read().decode("utf-8", errors="replace")
            if not raw:
                return {"ok": True}
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "status": exc.code, "error": raw or str(exc)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def post(path: str, body: dict[str, Any] | None = None) -> Any:
    return request_json("POST", path, body)


def get(path: str) -> Any:
    return request_json("GET", path)


def delayed_open(url: str, delay: float = 1.0) -> None:
    def run() -> None:
        time.sleep(delay)
        webbrowser.open(url)

    threading.Thread(target=run, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            self.send_html(page())
            return
        if self.path == "/api/status":
            self.send_json(
                {
                    "daemon": get("/api/daemon/status"),
                    "app": get("/api/apps/current-app-status"),
                    "motors": get("/api/motors/status"),
                    "media": get("/api/media/status"),
                }
            )
            return
        self.send_error(404)

    def do_POST(self) -> None:
        actions = {
            "/api/start-clawbody": self.start_clawbody,
            "/api/restart-clawbody": self.restart_clawbody,
            "/api/stop-app": self.stop_app,
            "/api/start-hand-tracker": self.start_hand_tracker,
            "/api/robot-sleep": self.robot_sleep,
            "/api/motors-on": self.motors_on,
            "/api/motors-off": self.motors_off,
            "/api/open-clawbody": lambda: self.open_url(CLAWBODY_UI),
            "/api/open-openclaw": lambda: self.open_url(OPENCLAW_UI),
            "/api/open-hand-tracker": lambda: self.open_url(HAND_TRACKER_UI),
        }
        action = actions.get(self.path)
        if action is None:
            self.send_error(404)
            return
        self.send_json(action())

    def start_clawbody(self) -> dict[str, Any]:
        result = post("/api/apps/start-app/clawbody")
        delayed_open(CLAWBODY_UI, 2.0)
        return {"action": "start-clawbody", "result": result}

    def restart_clawbody(self) -> dict[str, Any]:
        stop_result = post("/api/apps/stop-current-app")
        time.sleep(0.8)
        start_result = post("/api/apps/start-app/clawbody")
        delayed_open(CLAWBODY_UI, 2.0)
        return {"action": "restart-clawbody", "stop": stop_result, "start": start_result}

    def stop_app(self) -> dict[str, Any]:
        return {"action": "stop-app", "result": post("/api/apps/stop-current-app")}

    def start_hand_tracker(self) -> dict[str, Any]:
        stop_result = post("/api/apps/stop-current-app")
        time.sleep(0.8)
        start_result = post("/api/apps/start-app/hand_tracker_v2")
        delayed_open(HAND_TRACKER_UI, 2.0)
        return {"action": "start-hand-tracker", "stop": stop_result, "start": start_result}

    def robot_sleep(self) -> dict[str, Any]:
        stop_result = post("/api/apps/stop-current-app")
        time.sleep(0.5)
        move_result = post("/api/move/play/goto_sleep")
        time.sleep(1.0)
        motor_result = post("/api/motors/set_mode/disabled")
        return {
            "action": "robot-sleep",
            "stop": stop_result,
            "move": move_result,
            "motors": motor_result,
        }

    def motors_on(self) -> dict[str, Any]:
        return {"action": "motors-on", "result": post("/api/motors/set_mode/enabled")}

    def motors_off(self) -> dict[str, Any]:
        return {"action": "motors-off", "result": post("/api/motors/set_mode/disabled")}

    def open_url(self, url: str) -> dict[str, Any]:
        webbrowser.open(url)
        return {"action": "open-url", "url": url}

    def send_html(self, content: str) -> None:
        data = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: Any) -> None:
        data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def page() -> str:
    return """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Reachy Control Center</title>
  <style>
    :root { color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body { margin: 0; background: #f4f5f7; color: #20242b; }
    main { max-width: 920px; margin: 0 auto; padding: 28px; }
    h1 { font-size: 28px; margin: 0 0 4px; }
    .subtitle { margin: 0 0 24px; color: #626975; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }
    button, a.button {
      display: flex; align-items: center; justify-content: center; min-height: 64px;
      border: 1px solid #cfd4dc; border-radius: 8px; background: #fff; color: #20242b;
      font-size: 16px; font-weight: 650; text-decoration: none; cursor: pointer;
      box-shadow: 0 1px 2px rgba(0,0,0,.05);
    }
    button.primary { background: #ff8a1d; border-color: #ff8a1d; color: #fff; }
    button.danger { background: #2b2f36; border-color: #2b2f36; color: #fff; }
    button:disabled { opacity: .55; cursor: wait; }
    section { margin-top: 22px; }
    pre { white-space: pre-wrap; background: #111820; color: #d8e2ef; padding: 16px; border-radius: 8px; min-height: 180px; }
    .row { display: flex; gap: 12px; flex-wrap: wrap; }
    .row button { flex: 1 1 180px; }
  </style>
</head>
<body>
  <main>
    <h1>Reachy Control Center</h1>
    <p class="subtitle">Lokale Buttons für ClawBody, OpenClaw und Robot-Status.</p>

    <div class="grid">
      <button class="primary" data-action="/api/start-clawbody">ClawBody starten</button>
      <button data-action="/api/restart-clawbody">ClawBody neu starten</button>
      <button data-action="/api/start-hand-tracker">Tracking v2 starten</button>
      <button data-action="/api/stop-app">Aktuelle App stoppen</button>
      <button class="danger" data-action="/api/robot-sleep">Roboter schlafen</button>
      <button data-action="/api/motors-on">Motoren an</button>
      <button data-action="/api/motors-off">Motoren aus</button>
      <button data-action="/api/open-openclaw">OpenClaw öffnen</button>
      <button data-action="/api/open-clawbody">ClawBody UI öffnen</button>
      <button data-action="/api/open-hand-tracker">Tracking UI öffnen</button>
    </div>

    <section class="row">
      <button data-refresh>Status aktualisieren</button>
    </section>

    <section>
      <pre id="output">Lade Status...</pre>
    </section>
  </main>
  <script>
    const out = document.getElementById("output");
    async function showStatus() {
      const res = await fetch("/api/status");
      out.textContent = JSON.stringify(await res.json(), null, 2);
    }
    async function runAction(path, button) {
      button.disabled = true;
      out.textContent = "Aktion läuft: " + button.textContent;
      try {
        const res = await fetch(path, { method: "POST" });
        out.textContent = JSON.stringify(await res.json(), null, 2);
      } catch (error) {
        out.textContent = String(error);
      } finally {
        button.disabled = false;
        setTimeout(showStatus, 1200);
      }
    }
    document.querySelectorAll("[data-action]").forEach((button) => {
      button.addEventListener("click", () => runAction(button.dataset.action, button));
    });
    document.querySelector("[data-refresh]").addEventListener("click", showStatus);
    showStatus();
    setInterval(showStatus, 7000);
  </script>
</body>
</html>"""


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"Reachy Control Center: {url}")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
