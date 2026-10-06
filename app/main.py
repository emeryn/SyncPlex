import logging
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import auth
import config
import file_manager as fm
import plex_service as plex
from auth import Session, require_user
from download_manager import manager as downloads

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("syncplex")



class CachedStaticFiles(StaticFiles):
    """Assets are versioned with ?v=<app version> in the templates, so browsers can keep them."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=604800"
        return response


app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.mount("/static", CachedStaticFiles(directory=config.BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=config.BASE_DIR / "templates")

image_client = httpx.AsyncClient(headers=config.PLEX_HEADERS, timeout=20, follow_redirects=True)


# ---------------------------------------------------------------- models

class PinRequest(BaseModel):
    forward_url: str = ""


class DownloadRequest(BaseModel):
    server_id: str
    keys: List[str] = Field(min_length=1, max_length=2000)


class ConnectionRequest(BaseModel):
    uri: Optional[str] = None


class DeleteRequest(BaseModel):
    paths: List[str]


# ---------------------------------------------------------------- error handling

@app.exception_handler(plex.PlexError)
async def plex_error_handler(request: Request, exc: plex.PlexError):
    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


@app.exception_handler(httpx.HTTPError)
async def http_error_handler(request: Request, exc: httpx.HTTPError):
    log.warning("Upstream error on %s: %s", request.url.path, exc)
    return JSONResponse({"detail": f"Upstream error: {exc}"}, status_code=502)


@app.exception_handler(fm.FileError)
async def file_error_handler(request: Request, exc: fm.FileError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


# ---------------------------------------------------------------- pages

def _page(request: Request, name: str):
    return templates.TemplateResponse(request, name, {"version": config.APP_VERSION, "app_name": config.APP_NAME})


@app.get("/", include_in_schema=False)
def home(request: Request):
    if not auth.current_session(request):
        return RedirectResponse("/login", status_code=303)
    return _page(request, "index.html")


@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    if auth.current_session(request):
        return RedirectResponse("/", status_code=303)
    return _page(request, "login.html")


@app.get("/healthz", include_in_schema=False)
def healthcheck():
    return {"status": "ok"}


# ---------------------------------------------------------------- authentication

@app.post("/api/auth/pin", tags=["auth"])
def auth_create_pin(req: PinRequest, request: Request):
    forward_url = req.forward_url if req.forward_url.startswith(("http://", "https://")) else ""
    pin, nonce = auth.create_pin(forward_url)
    response = JSONResponse(pin)
    response.set_cookie(auth.LOGIN_COOKIE, nonce, max_age=auth.PIN_TTL, httponly=True, samesite="lax", secure=auth.is_https(request))
    return response


@app.get("/api/auth/pin/{pin_id}", tags=["auth"])
def auth_check_pin(pin_id: int, request: Request):
    token = auth.check_pin(pin_id, request.cookies.get(auth.LOGIN_COOKIE, ""))
    if not token:
        return {"status": "pending"}
    sid = auth.open_session(token)
    response = JSONResponse({"status": "ok"})
    response.set_cookie(
        auth.SESSION_COOKIE, sid, max_age=config.SESSION_TTL_DAYS * 86400,
        httponly=True, samesite="lax", secure=auth.is_https(request),
    )
    response.delete_cookie(auth.LOGIN_COOKIE)
    return response


@app.post("/api/auth/logout", tags=["auth"])
def auth_logout(request: Request):
    auth.store.delete(request.cookies.get(auth.SESSION_COOKIE))
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


@app.get("/api/auth/me", tags=["auth"])
def auth_me(user: Session = Depends(require_user)):
    return {
        "user": user.public,
        "version": config.APP_VERSION,
        "max_concurrent_downloads": config.MAX_CONCURRENT_DOWNLOADS,
    }


# ---------------------------------------------------------------- servers & libraries

@app.get("/api/servers", tags=["plex"])
def api_servers(user: Session = Depends(require_user)):
    return plex.list_servers(user.token)


@app.get("/api/servers/{server_id}/libraries", tags=["plex"])
def api_libraries(server_id: str, user: Session = Depends(require_user)):
    return plex.list_libraries(user.token, server_id)


@app.get("/api/servers/{server_id}/libraries/{section_id}", tags=["plex"])
def api_library(
    server_id: str,
    section_id: int,
    sort: str = "addedAt",
    direction: str = Query("desc", pattern="^(asc|desc)$"),
    start: int = Query(0, ge=0),
    size: int = Query(100, ge=1, le=500),
    q: str = "",
    user: Session = Depends(require_user),
):
    return plex.list_library(user.token, server_id, section_id, sort, direction, start, size, q.strip())


@app.get("/api/servers/{server_id}/recent", tags=["plex"])
def api_recent(server_id: str, user: Session = Depends(require_user)):
    return plex.recently_added(user.token, server_id)


@app.get("/api/servers/{server_id}/search", tags=["plex"])
def api_search(server_id: str, q: str = Query(min_length=1), user: Session = Depends(require_user)):
    return plex.search(user.token, server_id, q.strip())


@app.get("/api/servers/{server_id}/items/{key}", tags=["plex"])
def api_item(server_id: str, key: int, user: Session = Depends(require_user)):
    return plex.get_item(user.token, server_id, key)


@app.get("/api/servers/{server_id}/items/{key}/children", tags=["plex"])
def api_children(server_id: str, key: int, user: Session = Depends(require_user)):
    return plex.list_children(user.token, server_id, key)


@app.get("/api/servers/{server_id}/items/{key}/file", tags=["plex"])
def api_direct_file(server_id: str, key: int, user: Session = Depends(require_user)):
    """Redirects the browser to the original file on the Plex server (download to this device)."""
    info = plex.get_file_info(user.token, server_id, key)
    query = httpx.QueryParams({"download": 1, "X-Plex-Token": info["server_token"]})
    return RedirectResponse(f"{info['url']}?{query}", status_code=302)


@app.get("/api/servers/{server_id}/connections", tags=["plex"])
def api_connections(server_id: str, user: Session = Depends(require_user)):
    return plex.list_connections(user.token, server_id)


@app.put("/api/servers/{server_id}/connection", tags=["plex"])
def api_set_connection(server_id: str, req: ConnectionRequest, user: Session = Depends(require_user)):
    plex.set_connection(user.token, server_id, req.uri)
    return {"status": "ok"}


@app.get("/api/servers/{server_id}/image", tags=["plex"])
async def api_image(
    server_id: str,
    path: str,
    w: int = Query(300, ge=32, le=1000),
    h: int = Query(450, ge=32, le=1500),
    user: Session = Depends(require_user),
):
    """Resized poster proxy, so Plex tokens are never exposed to the browser."""
    if not path.startswith("/library/"):
        raise HTTPException(400, "Invalid image path")
    url, params, token = await run_in_threadpool(plex.image_target, user.token, server_id, path, w, h)
    r = await image_client.get(url, params=params, headers={"X-Plex-Token": token})
    if r.status_code != 200:
        raise HTTPException(404, "Image not found")
    return Response(
        r.content,
        media_type=r.headers.get("content-type", "image/jpeg"),
        # An explicit Content-Encoding makes GZipMiddleware skip already-compressed images.
        headers={"Cache-Control": "private, max-age=86400", "Content-Encoding": "identity"},
    )


# ---------------------------------------------------------------- downloads

@app.get("/api/downloads", tags=["downloads"])
def api_downloads(user: Session = Depends(require_user)):
    return downloads.status()


@app.post("/api/downloads", tags=["downloads"])
def api_download(req: DownloadRequest, user: Session = Depends(require_user)):
    with ThreadPoolExecutor(max_workers=8) as pool:
        expanded = list(pool.map(lambda k: plex.expand_keys(user.token, req.server_id, k), dict.fromkeys(req.keys)))
    keys = list(dict.fromkeys(k for group in expanded for k in group))
    queued = sum(downloads.enqueue(req.server_id, k, user.username, user.token) for k in keys)
    return {"queued": queued, "skipped": len(keys) - queued}


@app.post("/api/downloads/{action}", tags=["downloads"])
def api_downloads_action(action: str, user: Session = Depends(require_user)):
    actions = {
        "pause": lambda: downloads.set_paused(True),
        "resume": lambda: downloads.set_paused(False),
        "cancel-all": downloads.cancel_all,
        "clear-finished": downloads.clear_finished,
    }
    if action not in actions:
        raise HTTPException(400, "Unknown action")
    actions[action]()
    return downloads.status()


@app.post("/api/downloads/tasks/{task_id}/{action}", tags=["downloads"])
def api_task_action(task_id: str, action: str, user: Session = Depends(require_user)):
    actions = {"cancel": downloads.cancel, "retry": downloads.retry, "remove": downloads.remove}
    if action not in actions:
        raise HTTPException(400, "Unknown action")
    try:
        actions[action](task_id)
    except KeyError:
        raise HTTPException(404, "Download not found")
    return {"status": "ok"}


# ---------------------------------------------------------------- files

@app.get("/api/files", tags=["files"])
def api_files(path: str = "", user: Session = Depends(require_user)):
    return fm.list_directory(path)


@app.post("/api/files/delete", tags=["files"])
def api_files_delete(req: DeleteRequest, user: Session = Depends(require_user)):
    log.info("%s deleted %s", user.username, req.paths)
    return fm.delete_items(req.paths)


@app.get("/api/storage", tags=["files"])
def api_storage(user: Session = Depends(require_user)):
    return fm.storage()

