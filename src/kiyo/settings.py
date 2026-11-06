"""Editable local settings and safe process defaults."""

import os
import shutil
import tomllib
from pathlib import Path

BACKENDS = ("hermes", "openclaw", "claude")
PACKAGE = Path(__file__).resolve().parent


class KiyoError(RuntimeError):
    """An error that can be shown or spoken to the operator."""


class Settings:
    def __init__(self):
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        data_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        self.path = Path(os.environ.get("KIYO_CONFIG") or config_home / "kiyo/config.toml").expanduser()
        self.state = Path(os.environ.get("KIYO_STATE_DIR") or data_home / "kiyo").expanduser()
        try:
            self.values = tomllib.loads(self.path.read_text()) if self.path.exists() else {}
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise KiyoError(f"Kiyo konnte seine Einstellungen nicht lesen ({self.path}).") from exc

    def get(self, section, key, default=None):
        return self.values.get(section, {}).get(key, default)

    @property
    def default(self):
        backend = self.get("kiyo", "backend", "hermes")
        if backend not in BACKENDS:
            raise KiyoError("Der Standardagent in der Konfiguration ist unbekannt.")
        return backend

    @property
    def soul(self):
        path = Path(self.get("kiyo", "personality", str(PACKAGE / "SOUL.md"))).expanduser()
        if not path.is_absolute():
            path = self.path.parent / path
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise KiyoError(f"Kiyo konnte seine Persönlichkeit nicht laden ({path}).") from exc

    def executable(self, backend):
        configured = self.get(backend, "executable")
        if configured:
            return str(Path(configured).expanduser())
        return shutil.which(backend) or str(Path.home() / ".local/bin" / backend)
