"""Sign in with Plex (PIN flow) and server-side sessions.

The Plex token never leaves the server: the browser only holds an opaque session id.
"""
import json
import logging
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, Request

import config

log = logging.getLogger("syncplex.auth")

SESSION_COOKIE = "syncplex_session"
LOGIN_COOKIE = "syncplex_login"
PIN_TTL = 15 * 60


@dataclass
class Session:
    token: str
    user_id: int
    username: str
    email: str
    thumb: str
    created: float

    @property
    def public(self):
        return {"username": self.username, "email": self.email, "thumb": self.thumb}


class SessionStore:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.sessions: dict[str, Session] = {}
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            self.sessions = {sid: Session(**data) for sid, data in raw.items()}
            self._purge_expired()
        except Exception as e:
            log.warning("Could not load sessions: %s", e)

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({sid: asdict(s) for sid, s in self.sessions.items()}))
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def _purge_expired(self):
        limit = time.time() - config.SESSION_TTL_DAYS * 86400
        self.sessions = {sid: s for sid, s in self.sessions.items() if s.created > limit}

    def create(self, session: Session) -> str:
        sid = secrets.token_urlsafe(32)
        with self.lock:
            self._purge_expired()
            self.sessions[sid] = session
            self._save()
        return sid

    def get(self, sid):
        if not sid:
            return None
        session = self.sessions.get(sid)
        if session and session.created < time.time() - config.SESSION_TTL_DAYS * 86400:
            self.delete(sid)
            return None
        return session

    def delete(self, sid):
        with self.lock:
            if self.sessions.pop(sid, None):
                self._save()


store = SessionStore(config.DATA_DIR / "sessions.json")

# pin_id -> (login nonce held by the browser that created the PIN, created_at)
_pending_pins: dict[int, tuple[str, float]] = {}
_pins_lock = threading.Lock()


plex_tv = httpx.Client(base_url="https://plex.tv/api/v2", headers=config.PLEX_HEADERS, timeout=10)


def create_pin(forward_url: str):
    r = plex_tv.post("/pins", params={"strong": "true"})
    r.raise_for_status()
    pin = r.json()

    nonce = secrets.token_urlsafe(24)
    now = time.time()
    with _pins_lock:
        for pid in [p for p, (_, ts) in _pending_pins.items() if ts < now - PIN_TTL]:
            del _pending_pins[pid]
        _pending_pins[pin["id"]] = (nonce, now)

    params = {
        "clientID": config.CLIENT_ID,
        "code": pin["code"],
        "context[device][product]": config.APP_NAME,
    }
    if forward_url:
        # Used when the popup is blocked: Plex sends the user back to the login page, which resumes polling.
        params["forwardUrl"] = f"{forward_url}?pin={pin['id']}"
    auth_url = "https://app.plex.tv/auth#?" + urlencode(params)
    return {"id": pin["id"], "auth_url": auth_url}, nonce


def check_pin(pin_id: int, nonce: str):
    """Returns the Plex token once the user has approved the PIN, otherwise None."""
    with _pins_lock:
        pending = _pending_pins.get(pin_id)
    if not pending or not nonce or not secrets.compare_digest(pending[0], nonce):
        raise HTTPException(403, "Unknown or expired login attempt")

    r = plex_tv.get(f"/pins/{pin_id}")
    if r.status_code == 404:
        raise HTTPException(410, "Login attempt expired, please try again")
    r.raise_for_status()
    token = r.json().get("authToken")

    if token:
        with _pins_lock:
            _pending_pins.pop(pin_id, None)
    return token


def fetch_user(token: str):
    r = plex_tv.get("/user", headers={"X-Plex-Token": token})
    r.raise_for_status()
    return r.json()


def is_allowed(user: dict) -> bool:
    if not config.ALLOWED_USERS:
        return True
    names = {str(user.get(k, "")).lower() for k in ("username", "email", "title")}
    return bool(names & config.ALLOWED_USERS)


def open_session(token: str) -> str:
    user = fetch_user(token)
    if not is_allowed(user):
        log.warning("Rejected sign-in for Plex user %s", user.get("username"))
        raise HTTPException(403, f"The Plex account '{user.get('username')}' is not allowed on this SyncPlex instance")

    session = Session(
        token=token,
        user_id=user.get("id", 0),
        username=user.get("username") or user.get("title") or "Plex user",
        email=user.get("email", ""),
        thumb=user.get("thumb", ""),
        created=time.time(),
    )
    log.info("Plex user %s signed in", session.username)
    return store.create(session)


def current_session(request: Request):
    return store.get(request.cookies.get(SESSION_COOKIE))


def require_user(request: Request) -> Session:
    """FastAPI dependency protecting the API."""
    session = current_session(request)
    if not session:
        raise HTTPException(401, "Not authenticated")
    return session


def is_https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
