import os
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

APP_NAME = "SyncPlex"
APP_VERSION = "4.0.0"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "/config"))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/downloads"))

# Comma-separated Plex usernames or emails allowed to sign in. Empty = any Plex account.
ALLOWED_USERS = {u.strip().lower() for u in os.getenv("ALLOWED_USERS", "").split(",") if u.strip()}

SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "30"))
MAX_CONCURRENT_DOWNLOADS = max(1, int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "1")))

DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_client_id() -> str:
    """Plex tokens are bound to a client identifier, so it must be stable across restarts."""
    path = DATA_DIR / "client_id"
    if path.exists():
        value = path.read_text().strip()
        if value:
            return value
    value = f"syncplex-{uuid.uuid4()}"
    path.write_text(value)
    return value


CLIENT_ID = _load_client_id()

PLEX_HEADERS = {
    "Accept": "application/json",
    "X-Plex-Product": APP_NAME,
    "X-Plex-Version": APP_VERSION,
    "X-Plex-Client-Identifier": CLIENT_ID,
    "X-Plex-Platform": "Web",
    "X-Plex-Device-Name": APP_NAME,
}
