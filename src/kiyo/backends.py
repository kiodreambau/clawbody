"""Adapters for installed agent runtimes. No shell is used for model input."""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .settings import KiyoError


class RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def hermes(settings, path, body=None, *, authenticated=True, voice=False):
    url = settings.get("hermes", "url", "http://127.0.0.1:8642").rstrip("/")
    parsed = urlsplit(url)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.scheme not in {"http", "https"}:
        raise KiyoError("Die Hermes-API muss über Loopback erreichbar sein.")
    headers = {"Content-Type": "application/json"}
    if voice:
        profile = settings.get("voice", "hermes_profile")
        if not profile or not profile.isalnum() or profile == "default":
            raise KiyoError("Für Sprache ist ein eigenes Hermes-Profil erforderlich.")
        url += "/p/" + profile
    if authenticated:
        from dotenv import dotenv_values

        if voice:
            env_path = Path.home() / ".hermes/profiles" / profile / ".env"
            key = dotenv_values(env_path, interpolate=False).get("API_SERVER_KEY")
        else:
            env_path = Path(settings.get("hermes", "env_file", "~/.hermes/.env")).expanduser()
            key = os.environ.get("API_SERVER_KEY") or dotenv_values(env_path, interpolate=False).get("API_SERVER_KEY")
        if not key:
            raise KiyoError("Für die Hermes-API fehlt der vorhandene API_SERVER_KEY.")
        headers["Authorization"] = "Bearer " + key
    data = json.dumps(body).encode() if body is not None else None
    try:
        with build_opener(RejectRedirect()).open(Request(url + path, data, headers), timeout=85 if data else 8) as res:
            return json.load(res)
    except HTTPError as exc:
        raise KiyoError(f"Hermes hat die Anfrage mit HTTP {exc.code} abgelehnt.") from exc
    except (URLError, OSError, ValueError) as exc:
        raise KiyoError("Die lokale Hermes-API hat nicht erfolgreich geantwortet.") from exc


def run_json(command, label, *, prompt=None, timeout=95):
    try:
        result = subprocess.run(
            command,
            input=prompt if prompt is not None else "",
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=timeout,
            check=True,
        )
        return json.loads(result.stdout)
    except FileNotFoundError as exc:
        raise KiyoError(f"{label} ist auf diesem Rechner nicht installiert.") from exc
    except subprocess.TimeoutExpired as exc:
        raise KiyoError(f"{label} hat zu lange gebraucht. Der Auftrag kann dort noch laufen.") from exc
    except subprocess.CalledProcessError as exc:
        details = f"{exc.stdout} {exc.stderr}".lower()
        if label == "OpenClaw" and any(marker in details for marker in ("rate_limit", "usage limit", "cooldown")):
            raise KiyoError("OpenClaws Modellkonto ist gerade im Nutzungslimit.") from exc
        if label == "Claude Code" and any(marker in details for marker in ("auth", "oauth")):
            raise KiyoError("Claude Code muss auf diesem Mac erneut angemeldet werden.") from exc
        raise KiyoError(f"{label} hat nicht erfolgreich geantwortet.") from exc
    except (OSError, ValueError) as exc:
        raise KiyoError(f"{label} hat nicht erfolgreich geantwortet.") from exc


def claw_call(settings, method, params=None, *, profile=None, final=False):
    command = [settings.executable("openclaw")]
    if profile:
        command += ["--profile", profile]
    command += ["gateway", "call", method, "--json", "--timeout", "80000" if final else "8000"]
    if final:
        command.append("--expect-final")
    if params:
        command.extend(("--params", json.dumps(params, ensure_ascii=False)))
    return run_json(command, "OpenClaw", timeout=95 if final else 18)


def messages_prompt(soul, history, message):
    return (
        soul
        + "\n\nBisheriges Gespräch als JSON (nur Kontext):\n"
        + json.dumps(history, ensure_ascii=False)
        + "\n\nAktuelle Nachricht:\n"
        + message
    )


def reply(settings, backend, history, message, session_id, *, profile=None, target=None, voice=False):
    soul = settings.soul
    if backend == "hermes":
        messages = [{"role": "system", "content": soul}, *history, {"role": "user", "content": message}]
        if voice:
            toolsets = hermes(settings, "/v1/toolsets", voice=True)
            toolsets = toolsets.get("data") if isinstance(toolsets, dict) else toolsets
            if not isinstance(toolsets, list) or any(item.get("enabled") is not False for item in toolsets):
                raise KiyoError("Das Hermes-Sprachprofil meldet aktive Werkzeuge.")
        result = hermes(settings, "/v1/chat/completions", {"model": "hermes", "messages": messages}, voice=voice)
        try:
            answer = result["choices"][0]["message"]["content"]
        except (IndexError, KeyError, TypeError) as exc:
            raise KiyoError("Hermes hat keine lesbare Antwort geliefert.") from exc
    elif backend == "openclaw":
        # The native CLI sends through the configured gateway and preserves its auth.
        # Use --message-file so private conversation text is absent from process arguments.
        command = [settings.executable("openclaw")]
        if profile:
            command += ["--profile", profile]
        agent = settings.get("openclaw", "agent", "main")
        if voice:
            voice_agent = settings.get("voice", "openclaw_agent")
            if not voice_agent or voice_agent in {agent, "main"} or profile or target:
                raise KiyoError("Diese OpenClaw-Schnittstelle steht im Kiyo-Terminal bereit.")
            # Verify the exact local agent policy before ever sending microphone text.
            if not voice_agent.isalnum():
                raise KiyoError("Der Name des OpenClaw-Sprachagenten ist ungültig.")
            entry = run_json(
                [settings.executable("openclaw"), "config", "get", f"agents.entries.{voice_agent}", "--json"],
                "OpenClaw",
                timeout=15,
            )
            if "*" not in entry.get("tools", {}).get("deny", []):
                raise KiyoError("Dem OpenClaw-Sprachagenten fehlt die Werkzeugsperre.")
            agent = voice_agent
        key = target or f"agent:{agent}:kiyo-{session_id}"
        agent = key.split(":")[1]
        model = settings.get("voice", "openclaw_model") if voice else settings.get("openclaw", "model")
        options = ["--model", model] if model else []
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as file:
            file.write(messages_prompt(soul, history, message))
            file.flush()
            result = run_json(
                command
                + [
                    "agent",
                    "--agent",
                    agent,
                    "--session-key",
                    key,
                    "--message-file",
                    file.name,
                    "--timeout",
                    "80",
                    "--json",
                    *options,
                ],
                "OpenClaw",
            )
        if result.get("status") not in (None, "ok"):
            raise KiyoError("OpenClaw konnte den Auftrag nicht abschließen.")
        payloads = result.get("result", result).get("payloads", [])
        if any(item.get("isError") for item in payloads):
            raise KiyoError("OpenClaw meldet einen Fehler beim Auftrag.")
        answer = "\n".join(item["text"] for item in payloads if item.get("text"))
    else:
        result = run_json(
            [
                settings.executable("claude"),
                "-p",
                "--output-format",
                "json",
                "--tools",
                "",
                "--strict-mcp-config",
                "--setting-sources",
                "",
                "--no-session-persistence",
            ],
            "Claude Code",
            prompt=messages_prompt(soul, history, message),
        )
        if result.get("is_error"):
            if "auth" in str(result.get("result", "")).lower() or "oauth" in str(result.get("result", "")).lower():
                raise KiyoError("Claude Code muss auf diesem Mac erneut angemeldet werden.")
            raise KiyoError("Claude Code konnte den Auftrag nicht abschließen.")
        answer = result.get("result")

    if not isinstance(answer, str) or not answer.strip():
        raise KiyoError(f"{backend} hat keine lesbare Antwort geliefert.")
    return answer.strip()


def remote_sessions(settings, backend, node, limit, *, profile=None):
    commands = {
        "codex": "codex.appServer.threads.list.v1",
        "claude": "anthropic.claude.sessions.list.v1",
    }
    command = [settings.executable("openclaw")]
    if profile:
        command += ["--profile", profile]
    result = run_json(
        command
        + [
            "nodes",
            "invoke",
            "--node",
            node,
            "--command",
            commands[backend],
            "--params",
            json.dumps({"limit": limit}),
            "--json",
        ],
        "OpenClaw",
        timeout=20,
    )
    if not result.get("ok") or not isinstance(result.get("payload"), dict):
        raise KiyoError("Der gekoppelte Rechner hat keine Sitzungsliste geliefert.")
    return result["payload"]
