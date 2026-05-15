#!/usr/bin/env python3
"""
Porto Ao Vivo — scraper para agenda-porto.pt
Corre via GitHub Actions e gera events.json na raiz do repositório.
"""

import json
import re
import time
from datetime import datetime, date, timezone

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ─── Venues ──────────────────────────────────────────────────────────────────
VENUES = {
    "maus-habitos": {
        "name": "Maus Hábitos",
        "url": "https://www.agenda-porto.pt/en/local/nzYD78uzLNF/",
        "capacity": 150,
        "color": "#e8ff47",
    },
    "hard-club": {
        "name": "Hard Club",
        "url": "https://www.agenda-porto.pt/en/local/nKeROL8JlrK/",
        "capacity": 200,
        "color": "#a78bfa",
    },
    "rca": {
        "name": "RCA — Radioclube Agramonte",
        "url": "https://www.agenda-porto.pt/en/local/rca-radioclube-agramonte/",
        "capacity": 200,
        "color": "#ff6b35",
    },
    "understage": {
        "name": "Understage Rivoli",
        "url": "https://www.agenda-porto.pt/en/local/rivoli-theatre/",
        "capacity": 150,
        "color": "#34d399",
        "title_filter": "understage",
    },
}

MONTHS = {
    "jan": 1, "feb": 2, "fev": 2, "mar": 3,
    "apr": 4, "abr": 4, "may": 5, "mai": 5,
    "jun": 6, "jul": 7, "aug": 8, "ago": 8,
    "sep": 9, "set": 9, "oct": 10, "out": 10,
    "nov": 11, "dec": 12, "dez": 12,
}


_playwright = None
_browser = None

def get_browser():
    global _playwright, _browser
    if _browser is None:
        _playwright = sync_playwright().start()
        _browser = _playwright.chromium.launch(headless=True)
    return _browser

def fetch(url, retries=3):
    for attempt in range(retries):
        try:
            browser = get_browser()
            page = browser.new_page()
            page.set_extra_http_headers({
                "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
            })
            page.goto(url, wait_until="networkidle", timeout=30000)
            # Aguarda que apareçam links de eventos
            try:
                page.wait_for_selector('a[href*="/evento/"]', timeout=10000)
            except Exception:
                pass  # Pode não ter eventos — não é erro
            html = page.content()
            page.close()
            return html
        except Exception as e:
            print(f"  Tentativa {attempt+1}/3 falhou: {e}")
            time.sleep(2)
    return None


def parse_date(day_str, month_str):
    try:
        day = int(re.sub(r"\D", "", day_str or ""))
        month = MONTHS.get((month_str or "").strip().lower()[:3])
        if not month or not day:
            return None
        year = date.today().year
        ev_date = date(year, month, day)
        if (date.today() - ev_date).days > 30:
            ev_date = date(year + 1, month, day)
        return ev_date.isoformat()
    except Exception:
        return None


def scrape_venue(venue_id, info):
    print(f"\n-> {info['name']}")
    html = fetch(info["url"])
    if not html:
        print("  ERRO: pagina inacessivel")
        return []

    soup = BeautifulSoup(html, "html.parser")
    events = []

    # Each event card has data-bl-name="Card. Card Event".
    # The <a data-bl-name="Link evento"> is an empty overlay — all content
    # lives in sibling divs identified by data-bl-name attributes.
    for card in soup.find_all(attrs={"data-bl-name": "Card. Card Event"}):
        link_el = card.find(attrs={"data-bl-name": "Link evento"})
        if not link_el or not link_el.get("href"):
            continue
        href = link_el["href"]
        if "/evento/" not in href:
            continue

        title_el = card.find(attrs={"data-bl-name": "Title"})
        title_text = title_el.get_text(strip=True) if title_el else ""
        if not title_text:
            continue

        subtitle_el = card.find(attrs={"data-bl-name": "Subtitle"})
        subtitle = subtitle_el.get_text(strip=True) if subtitle_el else ""

        # Start Date contains two Text divs: day number and month abbreviation
        date_el = card.find(attrs={"data-bl-name": "Start Date"})
        date_texts = (
            [d.get_text(strip=True) for d in date_el.find_all(attrs={"data-bl-name": "Text"})]
            if date_el else []
        )
        day_str = date_texts[0] if len(date_texts) > 0 else ""
        month_str = date_texts[1] if len(date_texts) > 1 else ""
        date_iso = parse_date(day_str, month_str)
        if not date_iso:
            continue

        # Section link: href="/en/seccao/musica-e-clubbing/" or text "Music and clubbing"
        section_el = card.find(attrs={"data-bl-name": "Left"})
        section_href = section_el.get("href", "") if section_el else ""
        section_text = section_el.get_text(strip=True).lower() if section_el else ""
        is_music = (
            "musica-e-clubbing" in section_href
            or "music" in section_text
            or "clubbing" in section_text
        )
        if not is_music:
            continue

        tag_el = card.find(attrs={"data-bl-name": "Tag 2"})
        fmt = tag_el.get_text(strip=True) if tag_el else ""

        # Filtro por título (ex: Understage dentro do Rivoli)
        title_filter = info.get("title_filter")
        if title_filter and title_filter.lower() not in title_text.lower():
            continue

        slug = href.split("/evento/")[-1].strip("/")
        ev_id = re.sub(r"[^a-z0-9-]", "", slug)

        events.append({
            "id": ev_id,
            "title": title_text,
            "subtitle": subtitle,
            "venue_id": venue_id,
            "venue": info["name"],
            "venue_capacity": info["capacity"],
            "date": date_iso,
            "time": None,
            "type": fmt,
            "url": "https://www.agenda-porto.pt" + href,
            "intimate": info["capacity"] <= 200,
        })

    print(f"  {len(events)} eventos de musica encontrados")
    return events


def main():
    print("=" * 52)
    print("Porto Ao Vivo - Scraper")
    print(f"Data: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 52)

    all_events = []
    for venue_id, info in VENUES.items():
        events = scrape_venue(venue_id, info)
        all_events.extend(events)
        time.sleep(1.5)

    all_events.sort(key=lambda e: (e["date"], e["venue"]))

    seen, unique = set(), []
    for ev in all_events:
        if ev["id"] not in seen:
            seen.add(ev["id"])
            unique.append(ev)

    venues_public = {
        k: {kk: vv for kk, vv in v.items() if kk != "url"}
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

    # Fecha o browser
    global _browser, _playwright
    if _browser:
        _browser.close()
    if _playwright:
        _playwright.stop()


if __name__ == "__main__":
    main()
