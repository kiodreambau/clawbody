"""CLI shared by the robot and local terminal."""

import argparse
import json
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen

from . import backends, calendar
from .assistant import Assistant, transport
from .settings import BACKENDS, KiyoError, Settings


def build_parser():
    parser = argparse.ArgumentParser(prog="kiyo", description="Kiyo — Gespräch und Chief of Staff für Reachy Mini")
    commands = parser.add_subparsers(dest="command", required=True)
    ask = commands.add_parser("ask", help="Eine Nachricht an Kiyo")
    ask.add_argument("text", nargs="+")
    use = commands.add_parser("use", help="Agent und Schnittstelle für Sprache und Terminal wechseln")
    use.add_argument("backend", choices=BACKENDS)
    use.add_argument("--via")
    commands.add_parser("new", help="Ein neues Kiyo-Gespräch beginnen")
    commands.add_parser("status", help="Aktiven Agenten und Schnittstelle anzeigen")
    commands.add_parser("doctor", help="Reachy-Daemon, USB-Audio, Hermes und OpenClaw prüfen")
    commands.add_parser("wake", help="Den Reachy Mini aufrichten")
    commands.add_parser("sleep", help="Den Reachy Mini in Ruheposition fahren")
    commands.add_parser("robot", help="Über Mikrofon und Lautsprecher am Reachy Mini sprechen")
    audio_input_test = commands.add_parser(
        "microphone-test", help="Auf dem Reachy Mini einen Testsatz lokal transkribieren"
    )
    audio_input_test.add_argument("--seconds", type=int, choices=range(1, 11), default=5)
    robot_test = commands.add_parser("audio-test", help="Kurzen Testsatz am Reachy Mini ausgeben")
    robot_test.add_argument("text", nargs="*", default=["Hallo, ich bin Kiyo."])
    agenda = commands.add_parser("calendar", help="Termine lokal lesen")
    agenda.add_argument("day", nargs="?", choices=["heute", "morgen", "übermorgen", "woche"], default="heute")
    calendar_connect = commands.add_parser("calendar-connect", help="Google Kalender lesend verbinden")
    calendar_connect.add_argument("client_json")
    commands.add_parser("calendar-check", help="Google-Zugriff ohne Ausgabe von Termindetails prüfen")
    sessions = commands.add_parser("sessions", help="Agent-Sitzungen auflisten")
    sessions.add_argument("backend", choices=("openclaw", "hermes"), default="openclaw", nargs="?")
    sessions.add_argument("--via", default="lokal")
    sessions.add_argument("--parent", help="OpenClaw-Untersitzungen eines Schlüssels oder Alias anzeigen")
    history = commands.add_parser("session-history", help="Verlauf einer ausgewählten OpenClaw-Sitzung lesen")
    history.add_argument("session")
    history.add_argument("--via", default="lokal")
    history.add_argument("--limit", type=int, choices=range(1, 31), default=10)
    nodes = commands.add_parser("computers", help="Gekoppelte OpenClaw-Rechner anzeigen")
    nodes.add_argument("--via", default="lokal")
    remote = commands.add_parser("remote-sessions", help="Sitzungen eines gekoppelten Rechners auflisten")
    remote.add_argument("backend", choices=("codex", "claude"))
    remote.add_argument("--node", required=True)
    remote.add_argument("--via", default="lokal")
    remote.add_argument("--limit", type=int, choices=range(1, 31), default=10)
    commands.add_parser("tasks", help="Per Sprache vorgemerkte OpenClaw-Aufträge anzeigen")
    approve = commands.add_parser("approve", help="Einen vorgemerkten Auftrag freigeben und ausführen")
    approve.add_argument("id")
    delegate = commands.add_parser("delegate", help="Auftrag in eigener oder bestehender OpenClaw-Sitzung ausführen")
    delegate.add_argument("--via", default="lokal")
    destination = delegate.add_mutually_exclusive_group()
    destination.add_argument("--session", help="Bestehende Sitzung über Schlüssel oder Alias verwenden")
    destination.add_argument("--parent", help="Eine neue Untersitzung unter diesem Schlüssel oder Alias anlegen")
    delegate.add_argument("--node", help="Ausführung in einer neuen Sitzung an einen gekoppelten Rechner binden")
    delegate.add_argument("text", nargs="+")
    return parser


def doctor(settings):
    results = {}
    for name, probe in {
        "reachy": lambda: reachy_status(settings),
        "reachy_audio": lambda: reachy_audio_status(settings),
        "hermes": lambda: backends.hermes(settings, "/health", authenticated=False)["status"],
        "openclaw": lambda: backends.claw_call(settings, "health")["ok"],
    }.items():
        try:
            results[name] = probe()
        except (KiyoError, KeyError, ValueError):
            results[name] = "nicht erreichbar"
    return results


def reachy_audio_status(settings):
    try:
        import sounddevice
    except ImportError:
        return "Audio-Abhängigkeiten fehlen"

    from .robot import audio_device

    status = {}
    for direction in ("input", "output"):
        try:
            _, device = audio_device(settings, sounddevice, direction)
            status[direction] = device["name"]
        except (KiyoError, sounddevice.PortAudioError):
            status[direction] = "kein eindeutiges Reachy-Audiogerät"
    return status


def reachy_request(settings, path, method="GET"):
    port = int(settings.get("reachy", "port", 8000))
    try:
        request = Request(f"http://127.0.0.1:{port}/api/{path}", method=method)
        with urlopen(request, timeout=3) as res:
            return json.load(res)
    except (OSError, URLError, ValueError) as exc:
        raise KiyoError("Der lokale Reachy-Mini-Daemon ist nicht erreichbar.") from exc


def reachy_status(settings):
    data = reachy_request(settings, "daemon/status")
    return data.get("state") or data.get("status") or "unbekannt"


def dispatch(args, settings):
    assistant = Assistant(settings)
    command = args.command
    if command == "ask":
        return assistant.ask(" ".join(args.text))
    if command == "use":
        return assistant.use(args.backend, args.via)
    if command == "status":
        return assistant.status()
    if command == "new":
        return assistant.new()
    if command == "doctor":
        return doctor(settings)
    if command == "calendar":
        return calendar.agenda(settings, args.day)
    if command in {"wake", "sleep"}:
        if reachy_status(settings) != "running":
            raise KiyoError("Der Reachy-Daemon ist für Bewegungen nicht bereit.")
        if command == "wake" and reachy_request(settings, "motors/status").get("mode") != "enabled":
            reachy_request(settings, "motors/set_mode/enabled", "POST")
        move = "wake_up" if command == "wake" else "goto_sleep"
        return reachy_request(settings, "move/play/" + move, "POST")
    if command == "microphone-test":
        from .robot import microphone_test

        return microphone_test(settings, args.seconds)
    if command in {"robot", "audio-test"}:
        from .robot import listen, speak

        if command == "audio-test":
            speak(settings, " ".join(args.text))
            return "Testsatz über Reachy Mini ausgegeben."
        return listen(settings, assistant)
    if command == "calendar-connect":
        return calendar.authorize(settings, args.client_json)
    if command == "calendar-check":
        calendar.list_events(settings, "heute")
        return "Der Google-Kalender ist lesbar."
    if command == "tasks":
        return assistant.tasks()
    if command == "approve":
        return assistant.approve_task(args.id)
    if command == "sessions" and args.backend == "hermes":
        if args.parent:
            raise KiyoError("--parent ist für OpenClaw-Sitzungen verfügbar.")
        result = backends.hermes(settings, "/api/sessions?limit=20&include_children=true")
        return [{k: row.get(k) for k in ("id", "title", "parent_session_id")} for row in result.get("sessions", [])]
    profile = transport(settings, "openclaw", args.via)
    if command == "sessions":
        params = {"limit": 30}
        if args.parent:
            params["spawnedBy"] = backends.session_key(settings, args.parent)
        result = backends.claw_call(settings, "sessions.list", params, profile=profile)
        return [
            {
                k: row.get(k)
                for k in ("key", "label", "displayName", "kind", "parentSessionKey", "spawnedBy", "hasActiveRun")
            }
            for row in result.get("sessions", [])
        ]
    if command == "session-history":
        key = backends.session_key(settings, args.session)
        return backends.session_history(settings, key, args.limit, profile=profile)
    if command == "remote-sessions":
        result = backends.remote_sessions(settings, args.backend, args.node, args.limit, profile=profile)
        return [
            {key: row.get(key) for key in ("threadId", "name", "source", "status", "updatedAt")}
            for row in result.get("sessions", [])
        ]
    if command == "computers":
        result = backends.claw_call(settings, "node.list", profile=profile)
        return [{k: row.get(k) for k in ("displayName", "connected", "platform")} for row in result.get("nodes", [])]
    _, task = assistant.make_task(
        " ".join(args.text),
        session=args.session,
        parent=args.parent,
        node=args.node,
        profile=profile,
    )
    return assistant.run_task(task)


def main():
    args = build_parser().parse_args()
    try:
        result = dispatch(args, Settings())
    except KiyoError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    if isinstance(result, str):
        print(result)
    elif result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
