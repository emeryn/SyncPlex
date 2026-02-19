import cloudscraper
from bs4 import BeautifulSoup
import re
import concurrent.futures
import os

BASE_URL = os.getenv("WAWA_URL", "https://www.wawacity.irish")

URLS = {
    "movies": f"{BASE_URL}/?p=films&s=blu-ray_1080p-720p",
    "series": f"{BASE_URL}/?p=series&s=vf-hq",
    "mangas": f"{BASE_URL}/?p=mangas&s=vostfr"
}

def scrape_category(key, url):
    """Scrape une URL spécifique et retourne une liste d'items"""
    scraper = cloudscraper.create_scraper()
    print(f"DEBUG: Scraping {key} -> {url}")
    items = []
    
    try:
        html = scraper.get(url).text
        soup = BeautifulSoup(html, 'lxml')
        
        blocks = soup.find_all("div", class_="wa-sub-block")
        
        for block in blocks:
            try:
                title_div = block.find("div", class_="wa-sub-block-title")
                if not title_div: continue
                
                link_tag = title_div.find("a")
                title = link_tag.get_text(strip=True)
                href = link_tag['href']
                
                img_tag = block.find("img")
                thumb = ""
                if img_tag:
                    thumb = img_tag.get('src')
                    if thumb.startswith('/'): thumb = BASE_URL + thumb
                
                year = "2024"
                match = re.search(r'20[0-2][0-9]', block.get_text())
                if match: year = match.group(0)

                info_text = block.get_text().lower()
                quality = "HD"
                if "1080p" in info_text: quality = "1080p"
                elif "720p" in info_text: quality = "720p"
                elif "vostfr" in info_text: quality = "VOSTFR"
                
                items.append({
                    "title": title,
                    "year": year,
                    "quality": quality,
                    "thumb": thumb,
                    "link": BASE_URL + href,
                    "type": key # 'movies', 'series' ou 'mangas'
                })
            except: continue
            
    except Exception as e:
        print(f"Erreur scraping {key}: {e}")
        
    return key, items

def get_mixed_content():
    """Récupère Films, Séries et Mangas en parallèle"""
    results = {"movies": [], "series": [], "mangas": []}
    
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = [executor.submit(scrape_category, key, url) for key, url in URLS.items()]
        for future in concurrent.futures.as_completed(futures):
            key, items = future.result()
            results[key] = items
            
    return results

def get_real_link(page_url):
    """(Inchangé) Récupère le lien DL-Protect"""
    scraper = cloudscraper.create_scraper()
    try:
        html = scraper.get(page_url).text
        soup = BeautifulSoup(html, 'lxml')
        rows = soup.find_all("tr", class_="link-row")
        internal_link = None
        
        for row in rows:
            text_content = row.get_text().lower()
            if "1fichier" in text_content:
                link_tag = row.find("a", class_="link")
                if link_tag and 'href' in link_tag.attrs:
                    href = link_tag['href']
                    href = href.replace("&amp;", "&")
                    if not href.startswith('http'): href = BASE_URL + href
                    internal_link = href
                    break
        
        if not internal_link: return None

        try:
            resp = scraper.get(internal_link, allow_redirects=True)
            return resp.url if "wawacity" not in resp.url else internal_link
        except: return internal_link

    except: return None