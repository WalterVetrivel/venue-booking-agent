"""Sign-in. Only active accounts on the faculty allowlist get in; the session cookie holds their id."""

from fastapi import APIRouter, Depends, HTTPException, Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.db import get_db
from backend.models import Faculty

router = APIRouter(prefix="/auth", tags=["auth"])


class GoogleLogin(BaseModel):
    credential: str  # the ID token from Google Identity Services


class DevLogin(BaseModel):
    email: str


def _sign_in(request: Request, db: Session, email: str) -> Faculty:
    faculty = db.scalar(select(Faculty).where(func.lower(Faculty.email) == email.lower(), Faculty.active))
    if faculty is None:
        raise HTTPException(403, "This account is not on the faculty list. Ask the admin to add you.")
    request.session["faculty_id"] = faculty.id
    return faculty


def verify_google_token(credential: str) -> str:
    """Check a Google ID token and return its verified email address."""
    client_id = get_settings().google_client_id
    if not client_id:
        raise HTTPException(503, "Google Sign-In is not set up: GOOGLE_CLIENT_ID is missing.")
    try:
        info = id_token.verify_oauth2_token(credential, google_requests.Request(), client_id)
    except ValueError as e:
        raise HTTPException(401, "Google sign-in failed. Try again.") from e
    if not info.get("email_verified"):
        raise HTTPException(401, "That Google account's email is not verified.")
    return info["email"]


@router.get("/config")
def auth_config():
    s = get_settings()
    return {"google_client_id": s.google_client_id, "dev_login": s.dev_login}


@router.post("/google")
def google_login(body: GoogleLogin, request: Request, db: Session = Depends(get_db)):
    return _out(_sign_in(request, db, verify_google_token(body.credential)))


@router.post("/dev-login")
def dev_login(body: DevLogin, request: Request, db: Session = Depends(get_db)):
    if not get_settings().dev_login:
        raise HTTPException(404)
    return _out(_sign_in(request, db, body.email))


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


def current_user(request: Request, db: Session = Depends(get_db)) -> Faculty:
    faculty_id = request.session.get("faculty_id")
    faculty = db.get(Faculty, faculty_id) if faculty_id else None
    if faculty is None or not faculty.active:
        request.session.clear()
        raise HTTPException(401, "Sign in first.")
    return faculty


def require_admin(user: Faculty = Depends(current_user)) -> Faculty:
    if user.role != "admin":
        raise HTTPException(403, "Only the admin can do this.")
    return user


def _out(f: Faculty) -> dict:
    return {"id": f.id, "email": f.email, "name": f.name, "department": f.department, "role": f.role,
            "gmail_linked": f.gmail_linked}
