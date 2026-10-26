"""Google Drive: mirror the data folder into a local cache, using the admin's linked account (google_auth).

The folder named by DRIVE_ROOT_FOLDER_ID holds subfolders `circulars`, `reports` and `timetables`.
PDFs are downloaded as they are; Google Docs are exported as PDF. Files whose Drive checksum (or, for
Google Docs, modified time) hasn't changed are not downloaded again.
"""

import io
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from backend import google_auth
from backend.config import get_settings

KINDS = {"circulars": "circular", "reports": "report", "timetables": "timetable"}
FOLDER = "application/vnd.google-apps.folder"
GOOGLE_DOC = "application/vnd.google-apps.document"
PDF = "application/pdf"


def cache_dir() -> Path:
    return get_settings().data_dir / "drive_cache"


def credentials():
    return google_auth.system_credentials(google_auth.DRIVE)


@dataclass
class DriveFile:
    path: Path
    kind: str
    file_id: str
    modified_at: datetime


def _list(service, folder_id: str) -> list[dict]:
    files, page = [], None
    while True:
        r = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false", pageToken=page, pageSize=200,
            fields="nextPageToken, files(id, name, mimeType, md5Checksum, modifiedTime)").execute()
        files += r.get("files", [])
        page = r.get("nextPageToken")
        if not page:
            return files


def sync(folder_id: str | None = None) -> list[DriveFile]:
    """Download new or changed files from the Drive data folder. Returns every file now in the cache."""
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload

    folder_id = folder_id or get_settings().drive_root_folder_id
    if not folder_id:
        raise RuntimeError("Set DRIVE_ROOT_FOLDER_ID in .env.")
    service = build("drive", "v3", credentials=credentials(), cache_discovery=False)
    manifest_path = cache_dir() / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    out = []
    for sub in _list(service, folder_id):
        kind = KINDS.get(sub["name"].strip().lower())
        if sub["mimeType"] != FOLDER or kind is None:
            continue
        folder = cache_dir() / sub["name"].strip().lower()
        folder.mkdir(parents=True, exist_ok=True)
        for f in _list(service, sub["id"]):
            if f["mimeType"] not in (PDF, GOOGLE_DOC):
                continue
            name = f["name"] if f["name"].lower().endswith(".pdf") else f"{f['name']}.pdf"
            path = folder / name.replace("/", "-")
            version = f.get("md5Checksum") or f["modifiedTime"]
            if manifest.get(f["id"]) != version or not path.exists():
                request = service.files().get_media(fileId=f["id"]) if f["mimeType"] == PDF else \
                    service.files().export_media(fileId=f["id"], mimeType=PDF)
                buf = io.BytesIO()
                downloader = MediaIoBaseDownload(buf, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                path.write_bytes(buf.getvalue())
                manifest[f["id"]] = version
            out.append(DriveFile(path, kind, f["id"], datetime.fromisoformat(f["modifiedTime"].replace("Z", "+00:00"))))
    manifest_path.write_text(json.dumps(manifest, indent=1))
    return out
