"""Put the mock mails into your own Gmail inbox for testing. Nothing is sent to anyone.

Uses the Gmail API messages.insert call, which adds a message directly to your mailbox
(like an import), under a label "VBA-Mock" so the test mails are easy to find or delete.

Setup (once):
  1. In Google Cloud > your OAuth client (Web application), add the redirect URI
     http://localhost:8080/   (with the trailing slash).
  2. uv add google-api-python-client google-auth-oauthlib python-dotenv
Run from the project root:
  uv run python data/mock/mails/load_mock_mails.py
To remove them later: in Gmail, open the VBA-Mock label, select all, delete.
"""

import base64
import json
import os
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.insert", "https://www.googleapis.com/auth/gmail.labels"]
LABEL = "VBA-Mock"
HERE = Path(__file__).resolve().parent


def main():
    load_dotenv()
    me = os.environ["ADMIN_EMAIL"]
    client_file = os.environ.get("GOOGLE_OAUTH_CLIENT_FILE", "secrets/google_oauth_client.json")
    creds = InstalledAppFlow.from_client_secrets_file(client_file, SCOPES).run_local_server(port=8080)
    gmail = build("gmail", "v1", credentials=creds)

    labels = gmail.users().labels().list(userId="me").execute().get("labels", [])
    label_id = next((l["id"] for l in labels if l["name"] == LABEL), None)
    if not label_id:
        label_id = gmail.users().labels().create(userId="me", body={"name": LABEL}).execute()["id"]

    data = json.loads((HERE / "mock_mails.json").read_text(encoding="utf-8"))
    count = 0
    for thread in data["threads"]:
        gmail_thread_id = None
        last_sender = None
        for m in thread["messages"]:
            msg = EmailMessage()
            msg["From"] = me if m["from"] == "ADMIN" else m["from"]
            msg["To"] = last_sender if m["from"] == "ADMIN" else me
            msg["Subject"] = m["subject"]
            msg["Date"] = format_datetime(datetime.fromisoformat(m["date"]))
            msg["X-VBA-Mock-Thread"] = thread["id"]
            msg.set_content(m["body"])
            body = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode(),
                    "labelIds": ["INBOX", label_id] if m["from"] != "ADMIN" else ["SENT", label_id]}
            if gmail_thread_id:
                body["threadId"] = gmail_thread_id
            res = gmail.users().messages().insert(userId="me", body=body, internalDateSource="dateHeader").execute()
            gmail_thread_id = res["threadId"]
            if m["from"] != "ADMIN":
                last_sender = m["from"]
            count += 1
    print(f"Inserted {count} messages under the '{LABEL}' label.")


if __name__ == "__main__":
    main()
