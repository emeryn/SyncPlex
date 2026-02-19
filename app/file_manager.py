import os
import shutil

DOWNLOAD_DIR = "/downloads"

def list_directory(rel_path=""):
    """Liste le contenu du dossier de téléchargement"""
    if ".." in rel_path or rel_path.startswith("/"):
        rel_path = ""
        
    abs_path = os.path.join(DOWNLOAD_DIR, rel_path)
    
    if not os.path.exists(abs_path):
        return []

    items = []
    try:
        for entry in os.scandir(abs_path):
            size = entry.stat().st_size
            ftype = "file"
            if entry.is_dir():
                ftype = "folder"
            elif entry.name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov')):
                ftype = "video"
            
            items.append({
                "name": entry.name,
                "type": ftype,
                "size": size,
                "path": os.path.join(rel_path, entry.name),
                "rel_path": os.path.join(rel_path, entry.name)
            })
    except Exception as e:
        print(f"Error scanning directory: {e}")
        
    return items

def delete_items(paths):
    """Supprime fichiers/dossiers"""
    deleted_count = 0
    for p in paths:
        if ".." in p or p.startswith("/"): continue
        abs_path = os.path.join(DOWNLOAD_DIR, p)
        try:
            if os.path.isfile(abs_path): os.remove(abs_path)
            elif os.path.isdir(abs_path): shutil.rmtree(abs_path)
            deleted_count += 1
        except: pass
    return {"status": "ok", "deleted": deleted_count}

def parse_plex_item(item):
    director = "Unknown"
    try:
        if hasattr(item, 'directors') and item.directors:
            director = ", ".join([d.tag for d in item.directors])
    except: pass

    cast = []
    try:
        if hasattr(item, 'roles') and item.roles:
            cast = [r.tag for r in item.roles[:5]]
    except: pass
    rating = "N/A"
    try:
        val = getattr(item, 'rating', None)
        if val:
            rating = f"{float(val):.1f}"
    except: pass

    summary = getattr(item, 'summary', "No description available.")
    if not summary: summary = "No description available."

    return {
        "key": item.ratingKey,
        "title": item.title,
        "type": item.type,
        "year": getattr(item, 'year', None),
        "thumb": getattr(item, 'thumbUrl', None),
        "summary": summary,
        "rating": rating,
        "director": director,
        "cast": cast,
        "addedAt": item.addedAt.timestamp() if hasattr(item, 'addedAt') and item.addedAt else 0,
        "resolution": item.media[0].videoResolution if (hasattr(item, 'media') and item.media) else None,
        "size": item.media[0].parts[0].size if (hasattr(item, 'media') and item.media and item.media[0].parts) else 0,
    }