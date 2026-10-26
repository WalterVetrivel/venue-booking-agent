"""Google OAuth for two kinds of access, both through the web flow returning to /auth/google/callback.

- The system account: the admin links it once. Drive (read the data folder), Gmail send and Calendar
  (send notices and invites). Stored in secrets/google.token.json.
- Each faculty's own Gmail, read-only, linked by that faculty from the Assistant page. Stored in
  secrets/gmail/<faculty id>.token.json, and only ever used while that faculty is chatting.
"""

import json
from pathlib import Path

from backend.config import REPO_ROOT, get_settings

DRIVE = "https://www.googleapis.com/auth/drive.readonly"
GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"
CALENDAR = "https://www.googleapis.com/auth/calendar.events"
GMAIL_READ = "https://www.googleapis.com/auth/gmail.readonly"
SYSTEM_SCOPES = [DRIVE, GMAIL_SEND, CALENDAR]
FACULTY_SCOPES = [GMAIL_READ]


class NotLinked(Exception):
    pass


def _path(p: Path) -> Path:
    return p if p.is_absolute() else REPO_ROOT / p


def system_token_file() -> Path:
    return _path(get_settings().secrets_dir) / "google.token.json"


def faculty_token_file(faculty_id: int) -> Path:
    return _path(get_settings().secrets_dir) / "gmail" / f"{faculty_id}.token.json"


def flow(scopes: list[str], state: str | None = None):
    from google_auth_oauthlib.flow import Flow

    s = get_settings()
    client = _path(s.google_oauth_client_file)
    if not client.exists():
        raise FileNotFoundError(f"OAuth client file not found: {client}")
    return Flow.from_client_secrets_file(str(client), scopes=scopes, redirect_uri=s.google_oauth_redirect_uri,
                                         state=state)


def save(path: Path, credentials) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(credentials.to_json())


def granted(path: Path) -> set[str]:
    """Scopes a saved token was granted, or nothing when there is no token."""
    if not path.exists():
        return set()
    return set(json.loads(path.read_text()).get("scopes") or [])


def load(path: Path, scopes: list[str], what: str):
    """Valid credentials from a saved token, refreshed if needed. NotLinked when missing or revoked."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not path.exists():
        raise NotLinked(f"{what} isn't linked yet.")
    creds = Credentials.from_authorized_user_info(json.loads(path.read_text()), scopes)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as e:  # Testing-mode grants lapse after about a week
            raise NotLinked(f"{what} needs to be linked again (Google ended the access).") from e
        save(path, creds)
    return creds


def system_credentials(scope: str):
    if scope not in granted(system_token_file()):
        raise NotLinked("The Google account isn't linked for this yet. The admin links it on the Knowledge page.")
    return load(system_token_file(), SYSTEM_SCOPES, "The Google account")
