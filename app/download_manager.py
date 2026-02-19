import os
import requests
import plex_service as plex
import threading
import time
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

download_queue = []
active_downloads = {}
queue_lock = threading.Lock()
is_paused = False

def get_queue_status():
    return {
        "active": active_downloads,
        "queue_count": len(download_queue),
        "is_paused": is_paused
    }

def control_queue(action):
    global is_paused, download_queue, active_downloads
    
    if action == 'pause':
        is_paused = True
    elif action == 'resume':
        is_paused = False
    elif action == 'clear':
        with queue_lock:
            download_queue.clear()
            for k, v in active_downloads.items():
                if v['status'] == 'Pending':
                    active_downloads[k]['status'] = 'Cancelled'
            for k, v in active_downloads.items():
                if v['status'] == 'Downloading':
                    active_downloads[k]['status'] = 'Cancelling...'

def control_item(dl_id, action):
    global download_queue, active_downloads
    
    if action == 'delete':
        with queue_lock:
            download_queue = [t for t in download_queue if f"{t['server_id']}_{t['media_key']}" != dl_id]
        if active_downloads.get(dl_id, {}).get('status') == 'Downloading':
            active_downloads[dl_id]['status'] = 'Cancelling...'
        elif dl_id in active_downloads:
            del active_downloads[dl_id]

    elif action == 'cancel':
        with queue_lock:
            download_queue = [t for t in download_queue if f"{t['server_id']}_{t['media_key']}" != dl_id]
        if dl_id in active_downloads:
            if active_downloads[dl_id]['status'] == 'Downloading':
                active_downloads[dl_id]['status'] = 'Cancelling...'
            else:
                active_downloads[dl_id]['status'] = 'Cancelled'

    elif action == 'retry':
        if dl_id in active_downloads:
            active_downloads[dl_id]['status'] = 'Pending'
            active_downloads[dl_id]['progress'] = 0
            try:
                parts = dl_id.split('_')
                media_key = parts[-1]
                server_id = "_".join(parts[:-1])
                with queue_lock:
                    download_queue.append({'server_id': server_id, 'media_key': media_key})
            except: pass

def worker():
    while True:
        task = None
        if not is_paused:
            with queue_lock:
                if download_queue:
                    task = download_queue.pop(0)
        
        if task:
            process_download(task['server_id'], task['media_key'])
        else:
            time.sleep(1)

threading.Thread(target=worker, daemon=True).start()

def start_download(server_id, media_key):
    dl_id = f"{server_id}_{media_key}"
    if dl_id in active_downloads and active_downloads[dl_id]['status'] in ['Pending', 'Downloading']:
        return

    active_downloads[dl_id] = {
        "title": "Waiting...", 
        "status": "Pending", 
        "progress": 0, 
        "thumb": ""
    }
    
    with queue_lock:
        download_queue.append({'server_id': server_id, 'media_key': media_key})

def process_download(server_id, media_key):
    dl_id = f"{server_id}_{media_key}"
    
    try:
        if dl_id not in active_downloads: return 

        data = plex.get_real_file_url(server_id, media_key)
        download_url = data['url']
        filename = data['filename']
        source_name = data.get('server_name', 'Unknown')
        media_type = data.get('type', 'movie')
        
        base_dir = "/downloads"
        save_dir = os.path.join(base_dir, "tvshows" if media_type == 'episode' else "movies")
        if not os.path.exists(save_dir): os.makedirs(save_dir)
        filepath = os.path.join(save_dir, filename)

        active_downloads[dl_id] = {
            "title": data['title'], "status": "Downloading", 
            "progress": 0, "thumb": data['thumb'], "source": source_name
        }

        session = requests.Session()
        retries = Retry(total=5, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
        session.mount('https://', HTTPAdapter(max_retries=retries))

        with session.get(download_url, stream=True, timeout=20) as r:
            r.raise_for_status()
            total_size = int(r.headers.get('content-length', 0))
            downloaded_size = 0
            
            with open(filepath, 'wb') as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if active_downloads.get(dl_id, {}).get('status') == 'Cancelling...':
                        f.close()
                        if os.path.exists(filepath): os.remove(filepath)
                        active_downloads[dl_id]['status'] = 'Cancelled'
                        return 

                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if total_size > 0:
                            percent = int((downloaded_size / total_size) * 100)
                            if percent > active_downloads[dl_id]["progress"]:
                                active_downloads[dl_id]["progress"] = percent

        active_downloads[dl_id].update({"progress": 100, "status": "Finished", "path": filepath})
        
    except Exception as e:
        print(f"❌ ERROR: {e}")
        active_downloads[dl_id].update({"status": "Error", "error": str(e)[:100]})