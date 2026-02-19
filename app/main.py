from fastapi import FastAPI, BackgroundTasks, HTTPException, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.requests import Request
from fastapi.responses import HTMLResponse
from typing import List 
import file_manager as fm 
from pydantic import BaseModel
from typing import Optional, Union
import plex_service as plex
import download_manager as dm
import shutil 
import os
import requests
from dotenv import load_dotenv
import wawa_service

load_dotenv()
app = FastAPI(title="SYNCPLEX v3.5")

app.mount("/static", StaticFiles(directory="/app/static"), name="static")
templates = Jinja2Templates(directory="templates")

class DownloadRequest(BaseModel):
    server_id: str
    media_key: str

class ConnectionRequest(BaseModel):
    server_id: str
    uri: str

class DeleteRequest(BaseModel):
    paths: List[str]

def get_tmdb_data():
    api_key = os.getenv("TMDB_API_KEY")
    if not api_key:
        return {"status": "error", "message": "API Key missing"}

    base_url = "https://api.themoviedb.org/3"
    params = {"api_key": api_key, "language": "fr-FR", "region": "FR"}   
    try:
        upcoming = requests.get(f"{base_url}/movie/upcoming", params=params).json().get('results', [])[:10]
        trending = requests.get(f"{base_url}/trending/movie/week", params=params).json().get('results', [])[:10]
        popular = requests.get(f"{base_url}/movie/popular", params=params).json().get('results', [])[:10]
        return {"status": "ok", "upcoming": upcoming, "trending": trending, "popular": popular}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/wawa/latest")
def api_wawa_latest():
    """Renvoie un dict avec {movies: [], series: [], mangas: []}"""
    return {"data": wawa_service.get_mixed_content()}

@app.post("/api/wawa/link")
def api_wawa_link(payload: dict):
    """Récupère le lien protégé depuis l'URL de la fiche"""
    url = payload.get("url")
    link = wawa_service.get_real_link(url)
    if link:
        return {"status": "success", "link": link}
    return {"status": "error", "message": "Lien introuvable"}

@app.get("/api/system/logs")
def api_get_logs():
    return {"data": get_logs()}

@app.get("/api/tmdb")
def api_tmdb():
    return get_tmdb_data()

@app.post("/api/system/logs/clear")
def api_clear_logs():
    clear_logs()
    return {"status": "ok"}

@app.get("/api/files")
def api_list_files(path: str = ""):
    return {"data": fm.list_directory(path)}

@app.post("/api/files/delete")
def api_delete_files(req: DeleteRequest):
    return fm.delete_items(req.paths)

@app.get("/api/system/storage")
def api_storage():
    path = "/downloads"
    try:
        total, used, free = shutil.disk_usage(path)
    except:
        total, used, free = shutil.disk_usage("/")
        
    return {
        "total": total,
        "used": used,
        "free": free,
        "percent": int((used / total) * 100)
    }

@app.get("/api/servers")
def api_servers(): return plex.get_all_servers()

@app.get("/api/server/{server_id}/connections")
def api_get_connections(server_id: str): return plex.list_server_connections(server_id)

@app.post("/api/server/connection")
def api_set_connection(req: ConnectionRequest): return plex.set_preferred_connection(req.server_id, req.uri)

@app.get("/api/server/{server_id}/icon")
def api_server_icon(server_id: str):
    image_data = plex.get_server_icon(server_id)
    if image_data: return Response(content=image_data, media_type="image/png")
    else: raise HTTPException(status_code=404, detail="Icon not found")

@app.get("/api/server/{server_id}/direct_link")
def api_direct_link(server_id: str, media_key: str, stream: bool = False):
    data = plex.get_real_file_url(server_id, media_key)
    url = data['url']
    if not stream: url += "&download=1"
    return {"url": url, "filename": data['filename']}

@app.get("/api/server/{server_id}/search")
def api_search(server_id: str, section_id: int, query: str):
    return plex.search_content(server_id, section_id, query)

@app.get("/api/server/{server_id}")
def api_server_content(server_id: str, section_id: Optional[Union[int, str]]=None, parent_key: Optional[str]=None, sort_field: str="addedAt", sort_dir: str="desc"):
    return plex.get_server_content(server_id, section_id, parent_key, sort_field, sort_dir)

@app.post("/api/download")
def api_download(req: DownloadRequest, tasks: BackgroundTasks):
    dm.start_download(req.server_id, req.media_key)
    return {"status": "queued"}

@app.get("/api/downloads")
def api_downloads_status(): return dm.get_queue_status()

@app.post("/api/downloads/{action}")
def api_control_queue(action: str):
    if action not in ['pause', 'resume', 'clear']: raise HTTPException(400, "Invalid")
    dm.control_queue(action)
    return {"status": "ok"}

@app.post("/api/download/{dl_id}/{action}")
def api_control_item(dl_id: str, action: str):
    if action not in ['cancel', 'delete', 'retry']: raise HTTPException(400, "Invalid")
    dm.control_item(dl_id, action)
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})