from plexapi.myplex import MyPlexAccount
from plexapi import CONFIG
from plexapi.server import PlexServer
import requests
import os
import json

if 'headers' not in CONFIG.data: CONFIG.data['headers'] = {}
CONFIG.data['headers']['X-Plex-Client-Identifier'] = 'PlexThunderSync-Docker-ID'
CONFIG.data['headers']['X-Plex-Product'] = 'SYNCPLEX'
CONFIG.data['headers']['X-Plex-Version'] = '3.5'

PLEX_TOKEN = os.getenv("PLEX_TOKEN")
PLEX_USER = os.getenv("PLEX_USER")
PLEX_PASSWORD = os.getenv("PLEX_PASSWORD")

_account_cache = None
_forced_connections = {} 

def get_plex_account():
    global _account_cache
    if _account_cache: return _account_cache
    if PLEX_TOKEN: _account_cache = MyPlexAccount(token=PLEX_TOKEN)
    elif PLEX_USER and PLEX_PASSWORD: _account_cache = MyPlexAccount(PLEX_USER, PLEX_PASSWORD)
    else: raise Exception("Missing Credentials")
    return _account_cache


def get_base_url_and_token(client_identifier):
    account = get_plex_account()
    res = account.resource(client_identifier)
    token = res.accessToken
    if client_identifier in _forced_connections: return _forced_connections[client_identifier], token
    for conn in res.connections:
        try:
            requests.get(conn.uri, headers={'X-Plex-Token': token}, timeout=2)
            uri = conn.uri
            if not uri.endswith('/'): uri += '/'
            _forced_connections[client_identifier] = uri
            return uri, token
        except: continue
    raise Exception("No valid connection found.")

def list_server_connections(client_identifier):
    account = get_plex_account()
    res = account.resource(client_identifier)
    return [{"uri": c.uri, "local": c.local, "address": c.address} for c in res.connections]

def set_preferred_connection(client_identifier, uri):
    if not uri.endswith('/'): uri += '/'
    _forced_connections[client_identifier] = uri
    return {"status": "ok"}

def extract_languages(item):
    languages = set()
    file_path = ""
    if 'Media' in item:
        for media in item['Media']:
            for part in media.get('Part', []):
                file_path = part.get('file', '').upper()
    if file_path:
        if 'MULTI' in file_path: languages.add('MULTI')
        if any(x in file_path for x in ['TRUEFRENCH', 'VFF', 'FRENCH', 'VFQ']):
            languages.add('FRA')
        if 'VOSTFR' in file_path: languages.add('VOST')
        if 'ENGLISH' in file_path: languages.add('ENG')
    
    return list(languages) if languages else ["FRA"] 

def fetch_json(base_url, token, endpoint):
    url = f"{base_url}{endpoint}"

    if endpoint.startswith('/') and base_url.endswith('/'): url = f"{base_url}{endpoint[1:]}"
    headers = {'X-Plex-Token': token, 'Accept': 'application/json'}
    params = {'includeDetails': 1, 'includeStreams': 1}
    r = requests.get(url, headers=headers, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    return data

def get_real_file_url(client_identifier, rating_key):
    base_url, token = get_base_url_and_token(client_identifier)
    if not base_url.endswith('/'): base_url += '/'
    
    account = get_plex_account()
    server_name = account.resource(client_identifier).name

    data = fetch_json(base_url, token, f"library/metadata/{rating_key}")
    metadata = data['MediaContainer']['Metadata'][0]
    
    if 'Media' not in metadata: raise Exception("Not a file (Directory)")

    part = metadata['Media'][0]['Part'][0]
    part_key = part['key']
    if not part_key.startswith('/'): part_key = '/' + part_key
    
    clean_base = base_url.rstrip('/')
    final_url = f"{clean_base}{part_key}?X-Plex-Token={token}"
    
    title = metadata.get('title', 'Unknown')
    series = metadata.get('grandparentTitle')
    season = metadata.get('parentIndex')
    episode = metadata.get('index')
    ext = part.get('container', 'mkv')
    
    media_type = 'episode' if series else 'movie'

    if series:
        s_str = str(season).zfill(2)
        e_str = str(episode).zfill(2)
        filename = f"{series} - S{s_str}E{e_str} - {title}.{ext}"
    else:
        year = metadata.get('year', '')
        filename = f"{title} ({year}).{ext}"
        
    filename = "".join([c for c in filename if c.isalpha() or c.isdigit() or c in ' .()-_']).strip()
    
    return {
        "url": final_url, "filename": filename, "title": title,
        "thumb": f"{clean_base}{metadata.get('thumb','')}?X-Plex-Token={token}",
        "server_name": server_name,
        "type": media_type 
    }

def get_server_icon(client_identifier):
    try:
        base_url, token = get_base_url_and_token(client_identifier)
        img_url = f"{base_url.rstrip('/')}/photo/:/resources/server-icon.png?X-Plex-Token={token}&width=150&height=150"
        r = requests.get(img_url, stream=True, timeout=5)
        if r.status_code == 200: return r.content
    except: pass
    return None

def extract_media_info(item):
    size = 0
    resolution = ''
    if 'Media' in item:
        media = item['Media'][0]
        resolution = media.get('videoResolution', '')
        if str(resolution).isdigit(): resolution += 'p'
        if 'Part' in media:
            size = media['Part'][0].get('size', 0)
    return size, resolution

def extract_cast(item):
    """Extrait les 5 premiers acteurs"""
    cast = []
    if 'Role' in item:
        for actor in item['Role'][:5]: 
            cast.append(actor.get('tag', 'Unknown'))
    return cast

def get_server_content(client_identifier, section_id=None, parent_key=None, sort_field='addedAt', sort_dir='desc'):
    base_url, token = get_base_url_and_token(client_identifier)
    
    if section_id == 'recent':
        endpoint = "library/recentlyAdded?X-Plex-Container-Start=0&X-Plex-Container-Size=10"
        page_title = "Recently Added"
    elif section_id is None:
        data = fetch_json(base_url, token, "library/sections")
        sections = []
        for d in data['MediaContainer'].get('Directory', []):
            if d['type'] in ['movie', 'show']:
                sections.append({"id": d['key'], "title": d['title'], "type": d['type'], "count": 0})
        return {"type": "sections", "data": sections}
    else:
        if parent_key:
            endpoint = f"library/metadata/{parent_key}/children"
            try:
                fetch_json(base_url, token, endpoint)
                endpoint = f"library/metadata/{parent_key}/allLeaves"
            except: pass
        else:
            sort_map = {'title': 'titleSort', 'date': 'originallyAvailableAt', 'addedAt': 'addedAt'}
            real_sort = sort_map.get(sort_field, 'addedAt')
            endpoint = f"library/sections/{section_id}/all?sort={real_sort}:{sort_dir}&X-Plex-Container-Start=0&X-Plex-Container-Size=100"

    data = fetch_json(base_url, token, endpoint)
    container = data['MediaContainer']
    page_title = container.get('title1', 'Library')
    entries = container.get('Metadata', [])
    
    items = []
    for item in entries:
        thumb = item.get('thumb', '')
        thumb_url = f"{base_url.rstrip('/')}{thumb}?X-Plex-Token={token}" if thumb else ""
        
        display_title = item['title']
        if item['type'] == 'episode':
            s = str(item.get('parentIndex', 0)).zfill(2)
            e = str(item.get('index', 0)).zfill(2)
            display_title = f"S{s}E{e} - {item['title']}"

        size, res = extract_media_info(item)
        cast = extract_cast(item) 
        langs = extract_languages(item)
        items.append({
            "title": display_title,
            "year": item.get('year', ''),
            "thumb": thumb_url,
            "key": item['ratingKey'],
            "type": item['type'],
            "summary": item.get('summary', "No summary available.")[:500], 
            "size": size,
            "resolution": res,
            "cast": cast,
            "languages": langs
        })
        
    return {"type": "items", "data": items, "section_title": page_title}

def search_content(client_identifier, section_id, query):
    base_url, token = get_base_url_and_token(client_identifier)
    endpoint = f"library/sections/{section_id}/search?type=1&query={query}"
    data = fetch_json(base_url, token, endpoint)
    items = []
    for item in data['MediaContainer'].get('Metadata', []):
         size, res = extract_media_info(item)
         cast = extract_cast(item)
         langs = extract_languages(item)
         items.append({
            "title": item['title'],
            "year": item.get('year', ''),
            "thumb": f"{base_url.rstrip('/')}{item.get('thumb','')}?X-Plex-Token={token}",
            "key": item['ratingKey'],
            "type": item['type'],
            "summary": item.get('summary', "")[:500],
            "size": size,
            "resolution": res,
            "cast": cast,
            "languages": langs

        })
    return {"type": "search_results", "data": items, "section_title": f"Search: {query}"}

def get_all_servers():
    account = get_plex_account()
    servers = []
    for r in account.resources():
        if r.product == 'Plex Media Server':
            servers.append({
                "name": r.name, "id": r.clientIdentifier, 
                "owner": "Me" if r.owned else "Remote",
                "forced_url": _forced_connections.get(r.clientIdentifier, None)
            })
    return servers