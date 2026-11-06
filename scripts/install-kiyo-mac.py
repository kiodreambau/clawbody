#!/usr/bin/env python3
"""Generate Kiyo's local launchd definitions from this checkout."""

import argparse
import os
import plistlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REACHY = Path.home() / "Library/Application Support/com.pollen-robotics.reachy-mini/.venv/bin/reachy-mini-daemon"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", help="USB serial port for a separate local Reachy daemon")
    parser.add_argument("--replace", action="store_true", help="Replace previous Kiyo launch definitions")
    args = parser.parse_args()

    kiyo = ROOT / ".venv/bin/kiyo"
    if not kiyo.is_file():
        parser.error("Install Kiyo in this checkout's .venv first.")
    jobs = {"com.kiyo.voice": [str(kiyo), "robot"]}
    if args.serial:
        if not REACHY.is_file() or not Path(args.serial).exists():
            parser.error("The Reachy Mini daemon and USB serial port must exist.")
        jobs["com.kiyo.reachy"] = [
            str(REACHY),
            "--serialport",
            args.serial,
            "--fastapi-host",
            "127.0.0.1",
            "--fastapi-port",
            "8000",
            "--no-media",
            "--no-wake-up-on-start",
            "--no-goto-sleep-on-stop",
            "--dataset-update-interval",
            "0",
        ]

    launch_dir = Path.home() / "Library/LaunchAgents"
    log_dir = Path.home() / "Library/Logs/Kiyo"
    launch_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for label, command in jobs.items():
        path = launch_dir / f"{label}.plist"
        if path.exists() and not args.replace:
            parser.error(f"{path} already exists. Use --replace to update it.")
        for stream in ("out", "err"):
            log_path = log_dir / f"{label}.{stream}.log"
            if not log_path.exists():
                log_path.touch(mode=0o600)
        with path.open("wb") as file:
            plistlib.dump(
                {
                    "Label": label,
                    "ProgramArguments": command,
                    "WorkingDirectory": str(ROOT),
                    "RunAtLoad": True,
                    "KeepAlive": {"SuccessfulExit": False},
                    "ThrottleInterval": 30,
                    "StandardOutPath": str(log_dir / f"{label}.out.log"),
                    "StandardErrorPath": str(log_dir / f"{label}.err.log"),
                    "EnvironmentVariables": {
                        "PATH": f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/bin:/bin",
                        "HF_HUB_OFFLINE": "1",
                    },
                    "Umask": 0o077,
                },
                file,
            )
        print(f"launchctl bootstrap gui/{os.getuid()} {str(path)!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
