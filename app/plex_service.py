"""Thin client for plex.tv and Plex Media Server APIs."""
import logging
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import quote

import httpx

import config

log = logging.getLogger("syncplex.plex")

RESOURCES_TTL = 300
CONNECTION_TTL = 1800
PROBE_TIMEOUT = 4

SORT_FIELDS = {
    "addedAt": "addedAt",
    "title": "titleSort",
    "year": "year",
    "released": "originallyAvailableAt",
    "rating": "audienceRating",
}

http = httpx.Client(
    headers=config.PLEX_HEADERS,
    timeout=15,
    follow_redirects=True,
    transport=httpx.HTTPTransport(retries=2),
)

_resources_cache: dict[str, tuple[float, list]] = {}
_connections: dict[str, tuple[float, str]] = {}
_forced_connections: dict[str, str] = {}
_probe_locks = defaultdict(threading.Lock)


class PlexError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


class PlexAuthError(PlexError):
    def __init__(self, message="Your Plex session has expired, please sign in again"):
        super().__init__(message, 401)


@dataclass
class Server:
    id: str
    name: str
    uri: str
    token: str


# ---------------------------------------------------------------- plex.tv

def get_resources(token, refresh=False):
    cached = _resources_cache.get(token)
    if cached and not refresh and time.time() - cached[0] < RESOURCES_TTL:
        return cached[1]
    try:
        r = http.get(
            "https://clients.plex.tv/api/v2/resources",
            params={"includeHttps": 1, "includeRelay": 1},
            headers={"X-Plex-Token": token},
        )
    except httpx.HTTPError as e:
        raise PlexError(f"Could not reach plex.tv: {e}")
    if r.status_code == 401:
        raise PlexAuthError()
    r.raise_for_status()
    resources = [res for res in r.json() if "server" in (res.get("provides") or "")]
    _resources_cache[token] = (time.time(), resources)
    return resources


def _get_resource(token, server_id):
    for refresh in (False, True):
        for res in get_resources(token, refresh=refresh):
            if res["clientIdentifier"] == server_id:
                return res
    raise PlexError("Server not found on your Plex account", 404)


def list_servers(token):
    servers = []
    for res in get_resources(token):
        sid = res["clientIdentifier"]
        cached = _connections.get(sid)
        servers.append({
            "id": sid,
            "name": res.get("name", "Plex"),
            "owned": bool(res.get("owned")),
            "owner": "You" if res.get("owned") else (res.get("sourceTitle") or "Shared"),
            "online": bool(res.get("presence")),
            "version": res.get("productVersion", ""),
            "connection": _forced_connections.get(sid) or (cached[1] if cached else None),
            "forced": sid in _forced_connections,
        })
    servers.sort(key=lambda s: (not s["owned"], not s["online"], s["name"].lower()))
    return servers


# ---------------------------------------------------------------- connections

def _connection_rank(conn):
    if conn.get("relay"):
        return 2
    return 0 if conn.get("local") else 1


def _probe(uri, server_token, server_id):
    """A connection is valid only if it answers as the expected server (LAN IPs can collide)."""
    try:
        r = http.get(f"{uri.rstrip('/')}/identity", headers={"X-Plex-Token": server_token}, timeout=PROBE_TIMEOUT)
        return r.status_code == 200 and r.json()["MediaContainer"].get("machineIdentifier") == server_id
    except Exception:
        return False


def _resolve_uri(res):
    sid = res["clientIdentifier"]
    if sid in _forced_connections:
        return _forced_connections[sid]

    with _probe_locks[sid]:
        cached = _connections.get(sid)
        if cached and time.time() - cached[0] < CONNECTION_TTL:
            return cached[1]

        conns = sorted(res.get("connections") or [], key=_connection_rank)
        if conns:
            with ThreadPoolExecutor(max_workers=len(conns)) as pool:
                results = list(pool.map(lambda c: _probe(c["uri"], res.get("accessToken"), sid), conns))
            for conn, ok in zip(conns, results):
                if ok:
                    uri = conn["uri"].rstrip("/")
                    _connections[sid] = (time.time(), uri)
                    log.info("Using %s for server %s", uri, res.get("name"))
                    return uri

    raise PlexError(f"Server '{res.get('name')}' is unreachable")


def connect(token, server_id) -> Server:
    res = _get_resource(token, server_id)
    return Server(id=server_id, name=res.get("name", "Plex"), uri=_resolve_uri(res), token=res.get("accessToken") or token)


def list_connections(token, server_id):
    res = _get_resource(token, server_id)
    cached = _connections.get(server_id)
    current = _forced_connections.get(server_id) or (cached[1] if cached else None)
    return {
        "forced": server_id in _forced_connections,
        "connections": [
            {
                "uri": c["uri"].rstrip("/"),
                "address": c.get("address"),
                "port": c.get("port"),
                "local": bool(c.get("local")),
                "relay": bool(c.get("relay")),
                "active": c["uri"].rstrip("/") == current,
            }
            for c in sorted(res.get("connections") or [], key=_connection_rank)
        ],
    }


def set_connection(token, server_id, uri):
    """Pin a connection for a server, or go back to automatic detection when uri is empty."""
    res = _get_resource(token, server_id)
    _connections.pop(server_id, None)
    if not uri:
        _forced_connections.pop(server_id, None)
        return
    uri = uri.rstrip("/")
    # Only accept URIs advertised by plex.tv so the server token is never sent elsewhere.
    if uri not in {c["uri"].rstrip("/") for c in res.get("connections") or []}:
        raise PlexError("Unknown connection for this server", 400)
    _forced_connections[server_id] = uri


def server_get(server: Server, path, params=None):
    try:
        r = http.get(f"{server.uri}{path}", params=params, headers={"X-Plex-Token": server.token})
    except httpx.TransportError as e:
        _connections.pop(server.id, None)
        raise PlexError(f"Lost connection to '{server.name}': {e}")
    if r.status_code == 401:
        raise PlexError(f"Access denied by '{server.name}'", 403)
    if r.status_code == 404:
        raise PlexError("Item not found", 404)
    r.raise_for_status()
    return r.json().get("MediaContainer", {})


# ---------------------------------------------------------------- normalization

_RELEASE_TAGS = [
    ("MULTI", r"MULTI"),
    ("FR", r"TRUEFRENCH|FRENCH|VFF|VFQ|VF2"),
    ("SUB", r"VOSTFR|VOST|SUBBED"),
    ("HDR", r"HDR10\+?|HDR|DV|DOVI"),
    ("REMUX", r"REMUX"),
]


def release_tags(file_path):
    name = PurePosixPath(file_path.replace("\\", "/")).name.upper()
    return [tag for tag, pattern in _RELEASE_TAGS if re.search(rf"(?<![A-Z0-9])(?:{pattern})(?![A-Z0-9])", name)]


def image_url(server_id, path, width=300, height=450):
    if not path:
        return ""
    return f"/api/servers/{server_id}/image?path={quote(path, safe='')}&w={width}&h={height}"


def _resolution(media):
    res = str(media.get("videoResolution") or "")
    return f"{res}p" if res.isdigit() else res.upper()


def _first_media(m):
    media = (m.get("Media") or [{}])[0]
    part = (media.get("Part") or [{}])[0]
    return media, part


def normalize(server_id, m):
    kind = m.get("type")
    media, part = _first_media(m)
    poster = m.get("thumb")
    if kind == "episode":
        poster = m.get("parentThumb") or m.get("grandparentThumb") or m.get("thumb")
    elif kind == "season":
        poster = m.get("thumb") or m.get("parentThumb")

    item = {
        "key": str(m.get("ratingKey")),
        "server_id": server_id,
        "type": kind,
        "title": m.get("title", ""),
        "subtitle": "",
        "year": m.get("year"),
        "summary": m.get("summary", ""),
        "thumb": image_url(server_id, poster),
        "resolution": _resolution(media),
        "size": part.get("size", 0),
        "duration": m.get("duration", 0),
        "added_at": m.get("addedAt", 0),
        "rating": m.get("audienceRating") or m.get("rating"),
        "tags": release_tags(part.get("file", "")),
        "browsable": kind in ("show", "season"),
        "leaf_count": m.get("leafCount"),
    }

    if kind == "episode":
        season, episode = m.get("parentIndex") or 0, m.get("index") or 0
        item["title"] = f"S{season:02}E{episode:02} · {m.get('title', '')}"
        item["subtitle"] = m.get("grandparentTitle", "")
    elif kind == "season":
        item["subtitle"] = f"{m.get('parentTitle', '')} · {m.get('leafCount', 0)} episodes"
    elif kind == "show":
        item["subtitle"] = f"{m.get('childCount', 0)} seasons · {m.get('leafCount', 0)} episodes"
    else:
        item["subtitle"] = str(m.get("year") or "")
    return item


# ---------------------------------------------------------------- browsing

def list_libraries(token, server_id):
    server = connect(token, server_id)
    mc = server_get(server, "/library/sections")
    return {
        "server": server.name,
        "libraries": [
            {"id": d["key"], "title": d["title"], "type": d["type"]}
            for d in mc.get("Directory", [])
            if d.get("type") in ("movie", "show")
        ],
    }


def list_library(token, server_id, section_id, sort="addedAt", direction="desc", start=0, size=100, query=""):
    server = connect(token, server_id)
    params = {
        "sort": f"{SORT_FIELDS.get(sort, 'addedAt')}:{'asc' if direction == 'asc' else 'desc'}",
        "X-Plex-Container-Start": start,
        "X-Plex-Container-Size": size,
    }
    if query:
        params["title"] = query
    mc = server_get(server, f"/library/sections/{section_id}/all", params)
    return {
        "title": mc.get("librarySectionTitle") or mc.get("title1", ""),
        "total": mc.get("totalSize", mc.get("size", 0)),
        "items": [normalize(server_id, m) for m in mc.get("Metadata", [])],
    }


def list_children(token, server_id, key):
    server = connect(token, server_id)
    mc = server_get(server, f"/library/metadata/{key}/children")
    title = " · ".join(t for t in (mc.get("title1"), mc.get("title2")) if t)
    items = [normalize(server_id, m) for m in mc.get("Metadata", []) if m.get("type") in ("season", "episode")]
    return {"title": title, "total": len(items), "items": items}


def recently_added(token, server_id, size=30):
    server = connect(token, server_id)
    mc = server_get(server, "/library/recentlyAdded", {"X-Plex-Container-Start": 0, "X-Plex-Container-Size": size})
    items = [normalize(server_id, m) for m in mc.get("Metadata", []) if m.get("type") in ("movie", "show", "season", "episode")]
    for item in items:
        item["server_name"] = server.name
    return items


def search(token, server_id, query, limit=20):
    server = connect(token, server_id)
    mc = server_get(server, "/hubs/search", {"query": query, "limit": limit, "includeCollections": 0})
    items = []
    for hub in mc.get("Hub", []):
        for m in hub.get("Metadata", []):
            if m.get("type") in ("movie", "show", "season", "episode"):
                item = normalize(server_id, m)
                item["server_name"] = server.name
                items.append(item)
    return items


def _metadata(server, key):
    items = server_get(server, f"/library/metadata/{key}", {"includeExtras": 0}).get("Metadata")
    if not items:
        raise PlexError("Item not found", 404)
    return items[0]


def get_item(token, server_id, key):
    server = connect(token, server_id)
    m = _metadata(server, key)
    item = normalize(server_id, m)
    media, part = _first_media(m)
    streams = part.get("Stream", [])

    def languages(stream_type):
        seen = []
        for s in streams:
            if s.get("streamType") == stream_type:
                lang = s.get("language") or s.get("languageCode") or "Unknown"
                if lang not in seen:
                    seen.append(lang)
        return seen

    poster = m.get("thumb") if m.get("type") != "episode" else (m.get("parentThumb") or m.get("grandparentThumb"))
    item.update({
        "server_name": server.name,
        "poster": image_url(server_id, poster, 500, 750),
        "cast": [r.get("tag") for r in m.get("Role", [])[:10]],
        "directors": [d.get("tag") for d in m.get("Director", [])],
        "genres": [g.get("tag") for g in m.get("Genre", [])],
        "audio": languages(2),
        "subtitles": languages(3),
        "video_codec": (media.get("videoCodec") or "").upper(),
        "audio_codec": (media.get("audioCodec") or "").upper(),
        "container": (media.get("container") or "").upper(),
        "file": PurePosixPath(part.get("file", "").replace("\\", "/")).name,
        "plex_url": f"https://app.plex.tv/desktop/#!/server/{server_id}/details?key={quote('/library/metadata/' + item['key'], safe='')}",
    })
    return item


# ---------------------------------------------------------------- downloads

_INVALID_CHARS = re.compile(r'[<>"/\\|?*\x00-\x1f]')


def safe_name(value):
    value = _INVALID_CHARS.sub("", str(value or "").replace(": ", " - ").replace(":", "-"))
    return value.strip().rstrip(". ") or "Unknown"


def expand_keys(token, server_id, key):
    """Resolve a show or season to its episodes; movies and episodes resolve to themselves."""
    server = connect(token, server_id)
    kind = _metadata(server, key).get("type")
    if kind in ("movie", "episode"):
        return [str(key)]
    if kind in ("show", "season"):
        leaves = server_get(server, f"/library/metadata/{key}/allLeaves")
        return [str(m["ratingKey"]) for m in leaves.get("Metadata", [])]
    return []


def get_file_info(token, server_id, key):
    server = connect(token, server_id)
    m = _metadata(server, key)
    if m.get("type") not in ("movie", "episode") or not m.get("Media"):
        raise PlexError("This item is not a downloadable file", 400)

    media, part = _first_media(m)
    ext = PurePosixPath(part.get("file", "").replace("\\", "/")).suffix.lstrip(".") or part.get("container") or "mkv"
    title = m.get("title", "Unknown")

    if m["type"] == "episode":
        show = safe_name(m.get("grandparentTitle"))
        season, episode = m.get("parentIndex") or 0, m.get("index") or 0
        rel_path = PurePosixPath("tvshows", show, f"Season {season:02}", f"{show} - S{season:02}E{episode:02} - {safe_name(title)}.{ext}")
        display = f"{m.get('grandparentTitle')} · S{season:02}E{episode:02}"
        poster = m.get("parentThumb") or m.get("grandparentThumb")
    else:
        year = f" ({m['year']})" if m.get("year") else ""
        rel_path = PurePosixPath("movies", f"{safe_name(title)}{year}.{ext}")
        display = f"{title}{year}"
        poster = m.get("thumb")

    return {
        "url": f"{server.uri}{part['key']}",
        "server_token": server.token,
        "rel_path": str(rel_path),
        "filename": rel_path.name,
        "size": part.get("size", 0),
        "title": display,
        "thumb": image_url(server_id, poster, 120, 180),
        "server_name": server.name,
    }


def image_target(token, server_id, path, width, height):
    """URL and token used by the image proxy to fetch a resized poster from the server."""
    server = connect(token, server_id)
    params = {"url": path, "width": width, "height": height, "minSize": 1, "upscale": 1}
    return f"{server.uri}/photo/:/transcode", params, server.token
