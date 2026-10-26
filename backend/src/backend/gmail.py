"""Read a faculty member's own Gmail, only while they chat and only when they have linked it.

Nothing here stores mail. Search returns headers and Google's snippet; the full text of a thread is
fetched only for the one mail the faculty picks, and only that text goes to the language model.
"""

import base64
import html
import re
from dataclasses import asdict, dataclass

from backend import google_auth

MAX_TEXT = 8000  # characters of a thread passed on; booking details are near the top


@dataclass
class Mail:
    id: str
    thread_id: str
    sender: str
    subject: str
    date: str
    snippet: str


def linked(faculty_id: int) -> bool:
    return google_auth.GMAIL_READ in google_auth.granted(google_auth.faculty_token_file(faculty_id))


def unlink(faculty_id: int) -> None:
    google_auth.faculty_token_file(faculty_id).unlink(missing_ok=True)


def _service(faculty_id: int):
    from googleapiclient.discovery import build

    creds = google_auth.load(google_auth.faculty_token_file(faculty_id), google_auth.FACULTY_SCOPES, "Your Gmail")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def address(faculty_id: int) -> str:
    """The address of the linked mailbox, to check it is the faculty's own."""
    return _service(faculty_id).users().getProfile(userId="me").execute()["emailAddress"]


def _headers(msg: dict) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}


def _text(part: dict) -> str:
    """The plain-text body of a message, falling back to its HTML with the tags removed."""
    plain, rich = [], []

    def walk(p):
        data, mime = p.get("body", {}).get("data"), p.get("mimeType")
        if data and mime in ("text/plain", "text/html"):
            (plain if mime == "text/plain" else rich).append(
                base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace"))
        for sub in p.get("parts", []) or []:
            walk(sub)

    walk(part)
    if plain:
        return "\n".join(plain).strip()
    return html.unescape(re.sub(r"<[^>]+>", " ", "\n".join(rich))).strip()


class GoogleMail:
    """The real mailbox. Tests swap in their own object with the same two methods."""

    def search(self, faculty_id: int, query: str, max_results: int = 5) -> list[dict]:
        svc = _service(faculty_id)
        found = svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        out = []
        for m in found.get("messages", []):
            msg = svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                                             metadataHeaders=["From", "Subject", "Date"]).execute()
            h = _headers(msg)
            out.append(asdict(Mail(msg["id"], msg["threadId"], h.get("from", ""), h.get("subject", "(no subject)"),
                                   h.get("date", ""), html.unescape(msg.get("snippet", "")))))
        return out

    def read(self, faculty_id: int, message_id: str) -> dict:
        """The whole thread the message belongs to, oldest first, as plain text."""
        svc = _service(faculty_id)
        msg = svc.users().messages().get(userId="me", id=message_id, format="minimal").execute()
        thread = svc.users().threads().get(userId="me", id=msg["threadId"], format="full").execute()
        parts = []
        for m in thread.get("messages", []):
            h = _headers(m)
            parts.append(f"From: {h.get('from', '')}\nDate: {h.get('date', '')}\nSubject: {h.get('subject', '')}\n\n"
                         f"{_text(m.get('payload', {}))}")
        subject = _headers(thread["messages"][0]).get("subject", "(no subject)") if thread.get("messages") else ""
        return {"id": message_id, "subject": subject, "text": "\n\n---\n\n".join(parts)[:MAX_TEXT]}
