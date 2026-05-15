#!/usr/bin/env python3
"""
Porto Ao Vivo — scraper para agenda-porto.pt
Corre via GitHub Actions e gera events.json na raiz do repositório.
"""

import json
import re
import time
from datetime import datetime, date, timezone

import requests
from bs4 import BeautifulSoup

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
        "title_filter": "understage",  # só eventos com "understage" no título
    },
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.agenda-porto.pt/",
    "DNT": "1",
}

MONTHS = {
    "jan": 1, "feb": 2, "fev": 2, "mar": 3,
    "apr": 4, "abr": 4, "may": 5, "mai": 5,
    "jun": 6, "jul": 7, "aug": 8, "ago": 8,
    "sep": 9, "set": 9, "oct": 10, "out": 10,
    "nov": 11, "dec": 12, "dez": 12,
}


def fetch(url, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            print(f"  Tentativa {attempt+1}/{retries} falhou: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
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

    for link in soup.find_all("a", href=re.compile(r"^/en/evento/")):
        title_el = (
            link.find(class_=re.compile(r"event-?title", re.I))
            or link.find("h2")
            or link.find("h3")
        )
        if not title_el:
            texts = [t.strip() for t in link.stripped_strings if len(t.strip()) > 3]
            title_text = texts[0] if texts else ""
        else:
            title_text = title_el.get_text(strip=True)

        if not title_text:
            continue

        sub_el = link.find(class_=re.compile(r"event-?sub", re.I))
        subtitle = sub_el.get_text(strip=True) if sub_el else ""

        day_el = link.find(class_=re.compile(r"\bday\b", re.I))
        month_el = link.find(class_=re.compile(r"\bmonth\b", re.I))
        date_iso = parse_date(
            day_el.get_text(strip=True) if day_el else "",
            month_el.get_text(strip=True) if month_el else "",
        )

        hour_el = link.find(class_=re.compile(r"\bhour\b", re.I))
        time_str = hour_el.get_text(strip=True) if hour_el else None

        section_el = link.find(class_=re.compile(r"section-?name", re.I))
        format_el = link.find(class_=re.compile(r"format-?name", re.I))
        section = section_el.get_text(strip=True) if section_el else ""
        fmt = format_el.get_text(strip=True) if format_el else ""

        s = section.lower()
        f = fmt.lower()
        is_music = (
            "music" in s or "clubbing" in s or "musica" in s
            or f in {"concert", "party", "show", "listening", "dj set", "concerto", "festa"}
            or any(k in f for k in ("concert", "party", "dj", "listen"))
        )
        if not is_music:
            continue

        if not date_iso:
            continue

        # Filtro por título (ex: Understage dentro do Rivoli)
        title_filter = info.get("title_filter")
        if title_filter and title_filter.lower() not in title_text.lower():
            continue

        slug = link["href"].split("/evento/")[-1].strip("/")
        ev_id = re.sub(r"[^a-z0-9-]", "", slug)

        events.append({
            "id": ev_id,
            "title": title_text,
            "subtitle": subtitle,
            "venue_id": venue_id,
            "venue": info["name"],
            "venue_capacity": info["capacity"],
            "date": date_iso,
            "time": time_str,
            "type": fmt,
            "url": "https://www.agenda-porto.pt" + link["href"],
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


if __name__ == "__main__":
    main()
