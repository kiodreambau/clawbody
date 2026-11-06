"""Google Calendar read-only integration. Event content stays in Kiyo."""

import json
import os
import re
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .settings import KiyoError

SCOPES = ["https://www.googleapis.com/auth/calendar.events.readonly"]
CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars/"
READ_REQUEST = re.compile(
    r"\b(?:kalender|termine?)\b.*\b(?:heute|morgen|übermorgen|woche)\b"
    r"|\b(?:heute|morgen|übermorgen|woche)\b.*\b(?:kalender|termine?)\b"
    r"|^(?:lies|zeig|was steht|was habe|wie sieht).*\b(?:kalender|termine?)\b"
)
WRITE_REQUEST = re.compile(r"\b(?:buch\w*|verschieb\w*|lösch\w*|erstell\w*|trag\w*|sag\s+.+\s+ab)\b")
DAYS = ("heute", "morgen", "übermorgen", "woche")
WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")


def requested_day(text):
    text = text.casefold()
    if not READ_REQUEST.search(text) or WRITE_REQUEST.search(text):
        return None
    for word in ("übermorgen", "morgen", "woche"):
        if re.search(r"\b" + word + r"\b", text):
            return word
    return "heute"


def oauth_path(settings):
    return settings.state / "google-calendar.json"


def authorize(settings, client_file):
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from oauthlib.oauth2 import OAuth2Error
        from requests import RequestException
    except ImportError as exc:
        raise KiyoError("Installiere das Extra kiyo-calendar, um Google Kalender zu verbinden.") from exc
    try:
        client = json.loads(Path(client_file).expanduser().read_text())
        installed = client["installed"]
        if installed["auth_uri"] != "https://accounts.google.com/o/oauth2/auth":
            raise ValueError("Unexpected authorization endpoint")
        if installed["token_uri"] != "https://oauth2.googleapis.com/token":
            raise ValueError("Unexpected token endpoint")
        flow = InstalledAppFlow.from_client_config(client, SCOPES, autogenerate_code_verifier=True)
        credentials = flow.run_local_server(
            host="127.0.0.1",
            port=0,
            timeout_seconds=180,
            authorization_prompt_message="Google-Freigabe für Kiyo wurde im Browser geöffnet.",
            success_message="Google Kalender ist mit Kiyo verbunden. Du kannst dieses Fenster schließen.",
            prompt="consent",
        )
    except (OSError, ValueError, KeyError, OAuth2Error, RequestException) as exc:
        raise KiyoError(
            "Google Kalender konnte nicht verbunden werden. Prüfe die Desktop-OAuth-Datei und die Google-Freigabe."
        ) from exc
    if not credentials.refresh_token or not credentials.has_scopes(SCOPES):
        raise KiyoError("Google hat keine dauerhafte Lesefreigabe für den Kalender erteilt.")
    path = oauth_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = file.name
        try:
            file.write(credentials.to_json())
            file.flush()
            os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return "Google Kalender ist mit Kiyo verbunden."


def window(day, timezone, *, now=None):
    today = (now or datetime.now(timezone)).astimezone(timezone).date()
    offset = {"heute": 0, "morgen": 1, "übermorgen": 2, "woche": 0}[day]
    start = today + timedelta(days=offset)
    end = start + timedelta(days=7 if day == "woche" else 1)
    return datetime.combine(start, time.min, timezone), datetime.combine(end, time.min, timezone)


def list_events(settings, day):
    path = oauth_path(settings)
    if not path.is_file():
        raise KiyoError("Google Kalender braucht einmalig deine Lesefreigabe. Nutze kiyo calendar-connect im Terminal.")
    try:
        from google.auth.exceptions import GoogleAuthError
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2.credentials import Credentials
        from requests import RequestException
    except ImportError as exc:
        raise KiyoError("Die Google-Kalender-Bibliothek fehlt. Installiere das Extra kiyo-calendar.") from exc
    try:
        timezone = ZoneInfo(settings.get("calendar", "timezone", "Europe/Berlin"))
        credentials = Credentials.from_authorized_user_file(str(path), SCOPES)
        start, end = window(day, timezone)
        calendar_id = quote(settings.get("calendar", "id", "primary"), safe="")
        params = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "timeZone": timezone.key,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 100,
            "fields": "nextPageToken,items(summary,start,status)",
        }
        events = []
        with AuthorizedSession(credentials) as client:
            while len(events) < 1000:
                result = client.get(
                    CALENDAR_URL + calendar_id + "/events", params=params, timeout=10, allow_redirects=False
                )
                if result.status_code != 200:
                    if result.status_code == 403:
                        details = result.json().get("error", {}).get("details", [])
                        if any(detail.get("reason") == "SERVICE_DISABLED" for detail in details):
                            raise KiyoError("Die Calendar API ist im Google-Cloud-Projekt noch deaktiviert.")
                    raise KiyoError(f"Google Kalender hat die Anfrage mit HTTP {result.status_code} abgelehnt.")
                page = result.json()
                events.extend(item for item in page.get("items", []) if item.get("status") != "cancelled")
                token = page.get("nextPageToken")
                if not token or token == params.get("pageToken"):
                    break
                params["pageToken"] = token
    except (OSError, ValueError, GoogleAuthError, RequestException, ZoneInfoNotFoundError) as exc:
        raise KiyoError(
            "Kiyo konnte die Google-Termine nicht lesen. Prüfe die Freigabe mit kiyo calendar-connect."
        ) from exc
    return events, timezone


def format_events(events, timezone, day):
    if not events:
        return (
            "Im ausgewählten Kalender stehen "
            + ("für die nächsten sieben Tage" if day == "woche" else day)
            + " keine Termine."
        )
    parts = []
    for item in events[:12]:
        start = item.get("start", {})
        if "dateTime" in start:
            date = datetime.fromisoformat(start["dateTime"]).astimezone(timezone)
            when = date.strftime("%H:%M")
        elif "date" in start:
            date = datetime.fromisoformat(start["date"])
            when = "Ganztägig"
        else:
            continue
        title = " ".join(item.get("summary", "Termin ohne Titel").split())[:220]
        if day == "woche":
            when = f"{WEEKDAYS[date.weekday()]}, {date.day}.{date.month}., {when}"
        parts.append(f"{when}: {title}")
    if len(events) > 12:
        parts.append(f"Und {len(events) - 12} weitere Termine.")
    return "\n".join(parts) or "Google hat keine lesbaren Termindaten geliefert."


def agenda(settings, day):
    events, timezone = list_events(settings, day)
    return format_events(events, timezone, day)
