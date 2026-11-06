"""One conversation and a persistent backend choice shared by voice and CLI."""

import fcntl
import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager

from . import backends, calendar
from .settings import BACKENDS, KiyoError

NAMES = {"hermes": "Hermes", "openclaw": "OpenClaw", "claude": "Claude"}
AGENT = r"hermes|open\s*claw|claude|cloud|chlor"
SWITCH = re.compile(
    rf"(?:(?:wechsle|wechsel(?:e)?)\s+zu|nimm|nutze|benutz(?:e)?|verwende|sprich\s+mit|"
    rf"ich\s+will\s+mit)\s+({AGENT})\b(?:\s+reden)?"
    rf"(?:\s+(?:über|via)\s+([\w-]+))?(?:\s*[,.:!?]\s*(.*)|\s*)",
    re.IGNORECASE | re.DOTALL,
)
SINGLE = re.compile(rf"({AGENT})(?:\s+(?:über|via)\s+([\w-]+))?[.!?]?", re.IGNORECASE)
NEW = {"neues gespräch", "neue unterhaltung", "neue sitzung"}
TASK = re.compile(
    r"^(?:gib open\s*claw(?: über (?P<via>[\w-]+))?"
    r"(?: (?P<relation>in|unter) (?P<destination>[^\s,!?]+))? den auftrag"
    r"|auftrag an open\s*claw|beauftrage open\s*claw)(?:[,:.]\s*|\s+)(?P<text>.+)$",
    re.IGNORECASE | re.DOTALL,
)
COMPUTERS = re.compile(r"^(?:welche|zeig(?:e)?(?: mir)? die) (?:rechner|computer|openclaw-knoten)\b", re.IGNORECASE)
WHO = {"welcher agent", "welcher agent ist aktiv", "mit wem rede ich", "welches interface"}


def backend_name(value):
    name = re.sub(r"\s+", "", value.lower())
    return "claude" if name in {"cloud", "chlor"} else name


def transport(settings, backend, name):
    name = name.lower()
    if (backend, name) in {("hermes", "api"), ("claude", "cli"), ("openclaw", "lokal")}:
        return None
    if backend == "openclaw":
        profile = settings.get("openclaw_profiles", name)
        if profile:
            return profile
    raise KiyoError(f"Die Schnittstelle {name} ist für {NAMES[backend]} nicht eingerichtet.")


def transport_choices(settings, backend):
    if backend == "hermes":
        return ["API"]
    if backend == "claude":
        return ["CLI"]
    return ["lokal", *settings.values.get("openclaw_profiles", {})]


class Assistant:
    def __init__(self, settings):
        self.settings = settings

    @contextmanager
    def locked_state(self):
        root = self.settings.state
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / "conversation.json"
        fd = os.open(root / "conversation.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = (
                    json.loads(path.read_text())
                    if path.exists()
                    else {
                        "id": uuid.uuid4().hex,
                        "backend": self.settings.default,
                        "interface": None,
                        "pending": None,
                        "history": [],
                    }
                )
            except (ValueError, OSError) as exc:
                raise KiyoError("Kiyo konnte seinen Gesprächsstand nicht lesen.") from exc
            original = json.dumps(state)
            try:
                yield state
            finally:
                if json.dumps(state) != original:
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, delete=False) as file:
                        temporary = file.name
                        try:
                            json.dump(state, file, ensure_ascii=False)
                            file.flush()
                            os.fsync(file.fileno())
                            os.replace(temporary, path)
                        finally:
                            if os.path.exists(temporary):
                                os.unlink(temporary)

    def select(self, state, backend, interface=None):
        if backend not in BACKENDS:
            raise KiyoError("Unbekannter Agent.")
        choices = transport_choices(self.settings, backend)
        if not interface and self.settings.get("kiyo", "ask_interface_on_switch", True):
            state["pending"] = backend
            return f"Über welche Schnittstelle soll ich {NAMES[backend]} nutzen? " + " oder ".join(choices) + "?"
        interface = (interface or choices[0]).lower()
        transport(self.settings, backend, interface)
        state.update(backend=backend, interface=interface, pending=None)
        return (
            f"Ich nutze jetzt {NAMES[backend]} über {interface.upper() if interface in {'api', 'cli'} else interface}."
        )

    def use(self, backend, interface=None):
        with self.locked_state() as state:
            state.pop("pending_task", None)
            return self.select(state, backend, interface)

    def status(self):
        with self.locked_state() as state:
            return {key: state[key] for key in ("backend", "interface", "pending")}

    def new(self):
        with self.locked_state() as state:
            state.update(id=uuid.uuid4().hex, pending=None, history=[])
            state.pop("pending_task", None)
        return "Wir beginnen ein neues Gespräch."

    def make_task(self, text, *, session=None, parent=None, node=None, profile=None):
        if not text.strip():
            raise KiyoError("Gib einen OpenClaw-Auftrag an.")
        if session and (parent or node):
            raise KiyoError("Für eine bestehende Sitzung kannst du keinen neuen Elternchat oder Rechner festlegen.")
        session = backends.session_key(self.settings, session) if session else None
        parent = backends.session_key(self.settings, parent) if parent else None
        agent = parent.split(":")[1] if parent else self.settings.get("openclaw", "agent", "main")
        task_id = uuid.uuid4().hex[:12]
        key = session or backends.session_key(self.settings, f"agent:{agent}:kiyo-task-{task_id}")
        return task_id, {
            "text": text.strip(),
            "status": "pending",
            "session": key,
            "existing": session is not None,
            "parent": parent,
            "node": node,
            "profile": profile,
        }

    def stage_task(self, text, *, session=None, parent=None, via=None):
        if not text.strip() or len(text) > 4000:
            raise KiyoError("Ein vorgemerkter OpenClaw-Auftrag muss zwischen 1 und 4.000 Zeichen haben.")
        with self.locked_state() as state:
            tasks = state.setdefault("tasks", {})
            if len(tasks) >= 100:
                raise KiyoError("Kiyos Auftragsliste ist voll.")
            if not via and state["backend"] == "openclaw":
                via = state["interface"]
            profile = transport(self.settings, "openclaw", via) if via else None
            task_id, task = self.make_task(text, session=session, parent=parent, profile=profile)
            tasks[task_id] = task
        return task_id

    def run_task(self, task):
        profile = task.get("profile")
        key = task["session"]
        if task.get("existing"):
            backends.describe_session(self.settings, key, profile=profile)
        elif task.get("parent") or task.get("node"):
            backends.prepare_session(
                self.settings,
                key,
                parent=task.get("parent"),
                node=task.get("node"),
                profile=profile,
            )
        answer = backends.reply(self.settings, "openclaw", [], task["text"], "", target=key, profile=profile)
        return {"session": key, "reply": answer}

    def tasks(self):
        with self.locked_state() as state:
            return state.get("tasks", {})

    def approve_task(self, task_id):
        with self.locked_state() as state:
            task = state.get("tasks", {}).get(task_id)
            if not task or task["status"] != "pending":
                raise KiyoError("Dieser Auftrag wartet nicht auf eine Freigabe.")
            task["status"] = "running"
        try:
            result = self.run_task(task)
        except BaseException:
            with self.locked_state() as state:
                state["tasks"][task_id]["status"] = "needs-review"
            raise
        with self.locked_state() as state:
            state["tasks"][task_id]["status"] = "completed"
        return result

    def ask(self, text, *, voice=False):
        text = text.strip()
        if not text or len(text) > 16000:
            raise KiyoError("Bitte gib eine Nachricht zwischen 1 und 16.000 Zeichen ein.")
        text = re.sub(r"^\s*(?:hey\s+)?kiyo(?:[,:!?\s]+|$)", "", text, flags=re.IGNORECASE).strip()
        if not text:
            return "Ja, ich höre."
        normalized = text.lower().rstrip(".?!")
        if normalized in NEW:
            return self.new()
        task = TASK.fullmatch(text)
        if task:
            relation = task["relation"]
            self.stage_task(
                task["text"],
                session=task["destination"] if relation == "in" else None,
                parent=task["destination"] if relation == "unter" else None,
                via=task["via"],
            )
            return "Ich habe den OpenClaw-Auftrag vorgemerkt. Du findest ihn mit Kiyo Tasks im Terminal."
        if COMPUTERS.match(text):
            nodes = backends.claw_call(self.settings, "node.list").get("nodes", [])
            online = [node.get("displayName") or "Unbenannter Rechner" for node in nodes if node.get("connected")]
            return "Verbunden: " + ", ".join(online) + "." if online else "Gerade ist kein OpenClaw-Rechner verbunden."
        day = calendar.requested_day(text)
        if day:
            return calendar.agenda(self.settings, day)
        with self.locked_state() as state:
            if normalized in WHO:
                backend = state["backend"]
                interface = state["interface"] or transport_choices(self.settings, backend)[0]
                return f"Ich nutze {NAMES[backend]} über {interface}."
            match = SWITCH.fullmatch(text)
            if match:
                backend, interface, task = match.groups()
                confirmation = self.select(state, backend_name(backend), interface)
                if state["pending"] and task:
                    state["pending_task"] = task
                else:
                    state.pop("pending_task", None)
                if not task or state["pending"]:
                    return confirmation
                text = task
            elif state["pending"]:
                if normalized in {"abbrechen", "weiter wie bisher"}:
                    state["pending"] = None
                    state.pop("pending_task", None)
                    return "Wir bleiben bei " + NAMES[state["backend"]] + "."
                choice = re.sub(r"^(?:über|via)\s+", "", normalized)
                if choice not in [item.lower() for item in transport_choices(self.settings, state["pending"])]:
                    return self.select(state, state["pending"])
                confirmation = self.select(state, state["pending"], choice)
                text = state.pop("pending_task", None)
                if not text:
                    return confirmation
            else:
                match = SINGLE.fullmatch(text)
                if match:
                    return self.select(state, backend_name(match[1]), match[2])

            day = calendar.requested_day(text)
            if day:
                return calendar.agenda(self.settings, day)
            backend, interface = state["backend"], state["interface"]
            profile = transport(self.settings, backend, interface) if interface else None
            answer = backends.reply(
                self.settings,
                backend,
                state["history"],
                text,
                state["id"],
                profile=profile,
                voice=voice,
            )
            state["history"] = [
                *state["history"][-14:],
                {"role": "user", "content": text[:4000]},
                {"role": "assistant", "content": answer[:4000]},
            ]
            return answer
