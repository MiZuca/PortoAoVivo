#!/usr/bin/env python3
"""
Porto Ao Vivo — scraper para agenda-porto.pt
Corre via GitHub Actions e gera events.json na raiz do repositório.

Estratégia:
  Em vez de scrape por venue, faz-se scrape da secção "musica-e-clubbing"
  (que inclui TODOS os eventos musicais do Porto) e filtra-se por ref_local
  correspondente aos 4 venues pretendidos.  Desta forma apanha-se também
  eventos que não estão ligados à página do venue mas que têm a secção
  e o local correctos na base de dados.
"""

import json
import re
import time
from datetime import datetime, date, timezone

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ─── Configuração dos venues ──────────────────────────────────────────────────
# content_id: ID interno do bndlyr CMS (data-bl-content na página do venue)
VENUES = {
    "maus-habitos": {
        "name": "Maus Hábitos",
        "content_id": "nzYD78uzLNF",
        "url": "https://www.agenda-porto.pt/en/local/nzYD78uzLNF/",
        "capacity": 150,
        "color": "#e8ff47",
    },
    "hard-club": {
        "name": "Hard Club",
        "content_id": "nKeROL8JlrK",
        "url": "https://www.agenda-porto.pt/en/local/nKeROL8JlrK/",
        "capacity": 200,
        "color": "#a78bfa",
    },
    "rca": {
        "name": "RCA — Radioclube Agramonte",
        "content_id": "spmnjEPAYD7gQtPk",
        "url": "https://www.agenda-porto.pt/en/local/rca-radioclube-agramonte/",
        "capacity": 200,
        "color": "#ff6b35",
    },
    "understage": {
        "name": "Understage Rivoli",
        "content_id": "sFkX99zxmC1Yuzko",
        "url": "https://www.agenda-porto.pt/en/local/rivoli-theatre/",
        "capacity": 150,
        "color": "#34d399",
        "title_filter": "understage",
    },
}

# Mapa inverso: content_id → venue_id (para filtrar por ref_local)
_CONTENT_ID_TO_VENUE = {info["content_id"]: vid for vid, info in VENUES.items()}

MUSIC_SECTION_URL = "https://www.agenda-porto.pt/en/seccao/musica-e-clubbing/"
BONDLAYER_API = "https://repeater.bondlayer.com/fetch"
_HTTP = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
}

# ─── Playwright ───────────────────────────────────────────────────────────────
_playwright_inst = None
_browser = None


def get_browser():
    global _playwright_inst, _browser
    if _browser is None:
        _playwright_inst = sync_playwright().start()
        _browser = _playwright_inst.chromium.launch(headless=True)
    return _browser


def fetch(url, retries=3):
    for attempt in range(retries):
        try:
            browser = get_browser()
            page = browser.new_page()
            page.set_extra_http_headers({"Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8"})
            page.goto(url, wait_until="networkidle", timeout=30000)
            try:
                page.wait_for_selector("[data-bl-name]", timeout=8000)
            except Exception:
                pass
            html = page.content()
            page.close()
            return html
        except Exception as e:
            print(f"  Tentativa {attempt+1}/3 falhou: {e}")
            time.sleep(2)
    return None


# ─── bndlyr helpers ───────────────────────────────────────────────────────────

def _bndlyr_config(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", string=re.compile(r"BndLyrScripts"))
    if not script:
        return None
    t = script.string
    cfg_m = re.search(r"window\.BndDebug\s*=\s*(\{[^}]+\})", t)
    cjs_m = re.search(r'"(https://cdn\.bndlyr\.com/[^"]+content\.[^"]+\.js[^"]*)"', t)
    sjs_m = re.search(r'"(https://cdn\.bndlyr\.com/[^"]+struct\.js[^"]*)"', t)
    if not (cfg_m and cjs_m and sjs_m):
        return None
    cfg = json.loads(cfg_m.group(1))
    return {
        "project_id": cfg.get("projectId", ""),
        "content_id": cfg.get("contentId", ""),
        "hash": str(cfg.get("hash", "0")),
        "target": cfg.get("target", "production"),
        "content_js_url": cjs_m.group(1),
        "struct_js_url": sjs_m.group(1),
    }


def _events_repeater_id(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    # A secção de música usa "Flex Layout Events" (sem acento)
    el = soup.find(attrs={"data-bl-name": re.compile(r"Flex Layout Events?", re.I)})
    return el.get("data-repeater") if el else None


def _fetch_content_js(url: str) -> dict:
    r = requests.get(url, headers=_HTTP, timeout=20)
    m = re.match(r"window\.BndLyrContent\s*=\s*(\{.*\})\s*;?\s*$", r.text, re.DOTALL)
    return json.loads(m.group(1)) if m else {}


def _get_struct_text(url: str, _cache: dict = {}) -> str:
    if url not in _cache:
        r = requests.get(url, headers=_HTTP, timeout=30)
        _cache[url] = r.text
    return _cache[url]


def _repeater_def(struct_text: str, repeater_id: str) -> dict | None:
    key = f'"{repeater_id}":{{'
    idx = struct_text.find(key)
    if idx < 0:
        return None
    obj_start = struct_text.index("{", idx + len(f'"{repeater_id}":'))
    chunk = struct_text[obj_start:]
    depth = end = 0
    for i, ch in enumerate(chunk):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    try:
        return json.loads(chunk[:end])
    except Exception:
        return None


def _api_page(rdef: dict, page: int, cfg: dict, ev_content: dict) -> list:
    body_r = dict(rdef)
    body_r["page"] = page
    body_r["userSorts"] = ev_content.get("userSorts", {})
    body_r["userFilters"] = ev_content.get("userFilters", {})
    body = {
        "hash": cfg["hash"],
        "target": cfg["target"],
        "geoData": {"lat": 0, "lon": 0},
        "searchQuery": "",
        "favorites": {},
        "locale": "en",
        "contentId": cfg["content_id"],
        "projectId": cfg["project_id"],
        "repeater": body_r,
    }
    hdrs = {
        **_HTTP,
        "Content-Type": "application/json",
        "Origin": "https://www.agenda-porto.pt",
        "Referer": MUSIC_SECTION_URL,
    }
    try:
        resp = requests.post(BONDLAYER_API, json=body, headers=hdrs, timeout=20)
        return resp.json().get("items", []) if resp.status_code == 200 else []
    except Exception:
        return []


def _item_to_event(item: dict) -> dict | None:
    # Determina o venue pelo ref_local (content_id do venue)
    ref_local = item.get("ref_local")
    venue_id = _CONTENT_ID_TO_VENUE.get(ref_local)
    if not venue_id:
        return None
    info = VENUES[venue_id]

    # Título: text_display_title é o título limpo; strip de prefixo [Venue] caso exista
    title_data = item.get("text_display_title") or item.get("_title") or {}
    title = (title_data.get("en") or title_data.get("all") or "") if isinstance(title_data, dict) else ""
    title = re.sub(r"^\[[^\]]+\]\s*-\s*", "", title).strip()
    if not title:
        return None

    # Filtro por título (ex: Understage dentro do Rivoli)
    tf = info.get("title_filter")
    if tf and tf.lower() not in title.lower():
        return None

    sub_data = item.get("text_subtitle") or {}
    subtitle = (sub_data.get("en") or sub_data.get("all") or "") if isinstance(sub_data, dict) else ""

    start_str = item.get("datetime_start_date", "")
    if not start_str:
        return None
    try:
        # Horários em hora local portuguesa (não UTC)
        dt = datetime.fromisoformat(start_str.replace("Z", ""))
        date_iso = dt.date().isoformat()
        hide_hour = item.get("boolean_esconder_hora", False)
        time_str = None if hide_hour else dt.strftime("%H:%M")
    except Exception:
        return None

    # Descarta eventos com mais de 1 ano no futuro (provavelmente erro de dados)
    try:
        if (date.fromisoformat(date_iso) - date.today()).days > 365:
            return None
    except Exception:
        pass

    slug_data = item.get("_slug") or {}
    slug = (slug_data.get("all") or slug_data.get("en") or "") if isinstance(slug_data, dict) else ""
    if not slug:
        return None

    ev_id = re.sub(r"[^a-z0-9-]", "", slug)
    return {
        "id": ev_id,
        "title": title,
        "subtitle": subtitle,
        "venue_id": venue_id,
        "venue": info["name"],
        "venue_capacity": info["capacity"],
        "date": date_iso,
        "time": time_str,
        "type": "",
        "url": f"https://www.agenda-porto.pt/en/evento/{slug}/",
        "intimate": info["capacity"] <= 200,
    }


# ─── Scraper principal ────────────────────────────────────────────────────────

def scrape_all() -> list:
    """Faz scrape de todos os eventos musicais e filtra pelos 4 venues."""
    print(f"\nA carregar secção musica-e-clubbing...")
    html = fetch(MUSIC_SECTION_URL)
    if not html:
        print("  ERRO: secção inacessível")
        return []

    cfg = _bndlyr_config(html)
    repeater_id = _events_repeater_id(html)
    if not cfg or not repeater_id:
        print("  ERRO: config bndlyr não encontrada")
        return []

    print(f"  repeater: {repeater_id}")

    content_data = _fetch_content_js(cfg["content_js_url"])
    ev_content = content_data.get(repeater_id, {})
    page1_items = ev_content.get("items", [])
    total_pages = ev_content.get("totalPages", 1)
    print(f"  página 1: {len(page1_items)} items, total páginas: {total_pages}")

    struct_text = _get_struct_text(cfg["struct_js_url"])
    rdef = _repeater_def(struct_text, repeater_id)

    all_items = list(page1_items)
    if rdef and total_pages > 1:
        for page in range(2, total_pages + 1):
            items = _api_page(rdef, page, cfg, ev_content)
            if not items:
                break
            all_items.extend(items)
            print(f"  página {page}: +{len(items)} items")
            time.sleep(0.3)

    print(f"  total bruto: {len(all_items)} eventos musicais")

    events = []
    for item in all_items:
        ev = _item_to_event(item)
        if ev:
            events.append(ev)

    # Conta por venue
    counts = {}
    for ev in events:
        counts[ev["venue"]] = counts.get(ev["venue"], 0) + 1
    for venue, n in counts.items():
        print(f"  {venue}: {n} eventos")

    return events


def main():
    print("=" * 52)
    print("Porto Ao Vivo - Scraper")
    print(f"Data: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 52)

    all_events = scrape_all()
    all_events.sort(key=lambda e: (e["date"], e["venue"]))

    seen, unique = set(), []
    for ev in all_events:
        if ev["id"] not in seen:
            seen.add(ev["id"])
            unique.append(ev)

    venues_public = {
        k: {kk: vv for kk, vv in v.items() if kk not in ("content_id",)}
        for k, v in VENUES.items()
    }

    output = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": len(unique),
        "venues": venues_public,
        "events": unique,
    }

    with open("events.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\nTotal: {len(unique)} eventos guardados em events.json")
    print(f"Actualizado em: {output['updated_at']}")

    global _browser, _playwright_inst
    if _browser:
        _browser.close()
    if _playwright_inst:
        _playwright_inst.stop()


if __name__ == "__main__":
    main()
