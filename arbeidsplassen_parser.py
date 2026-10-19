#!/usr/bin/env python3
"""Парсер вакансий: фаза 1 - arbeidsplassen.nav.no, фаза 2 - finn.no

Установка:
    pip install beautifulsoup4 playwright
    playwright install chromium

Запуск:
    python arbeidsplassen_parser.py --pages 5 --out jobs                # обе фазы -> jobs.json + jobs.csv
    python arbeidsplassen_parser.py --source finn --pages 3 --out jobs  # только FINN (nav / finn / all)
    python arbeidsplassen_parser.py --html card.html --out jobs # только разбор готового HTML
"""
import argparse
import csv
import json
import re
from urllib.parse import urlencode

from bs4 import BeautifulSoup

BASE = "https://arbeidsplassen.nav.no"
LINK_SEL = 'h2 a[href^="/stillinger/stilling/"]'

# Те же фильтры, что в твоей ссылке
FILTERS = [
    ("county", "TELEMARK"),
    ("v", "6"),
    ("education", "Ingen krav"),
    ("education", "Videregående"),
    ("experience", "ingen krav til arbeidserfaring"),
    ("experience", "noe arbeidserfaring"),
    ("needDriversLicense", "false"),
    ("needDriversLicense", "Ikke oppgitt"),
    ("workLanguage", "Norsk"),
    ("extent", "Deltid"),
    ("engagementType", "Engasjement"),
    ("engagementType", "Sesong"),
    ("engagementType", "Vikariat"),
    ("engagementType", "Prosjekt"),
    ("engagementType", "Annet"),
]


def search_url(page: int = 1) -> str:
    params = list(FILTERS)
    if page > 1:
        params.append(("from", str((page - 1) * 25)))  # при необходимости поправь по реальному URL пагинации
    return f"{BASE}/stillinger?{urlencode(params)}"


def _after_hidden_label(card, label):
    """Текст <p> рядом со скрытой подписью (Arbeidsgiver / Sted)."""
    for p in card.select("p.aksel-typo--visually-hidden"):
        if p.get_text(strip=True) == label:
            row = p.find_parent("div", class_="aksel-hstack")
            if row:
                texts = [x.get_text(" ", strip=True) for x in row.find_all("p")
                         if "visually-hidden" not in " ".join(x.get("class", []))]
                return texts[0] if texts else None
    return None


def parse_cards(html: str):
    soup = BeautifulSoup(html, "html.parser")
    jobs, seen = [], set()
    for a in soup.select(LINK_SEL):
        href = a["href"]
        if href in seen:
            continue
        seen.add(href)

        # поднимаемся до контейнера, внутри которого ровно одна вакансия
        card = a
        while card.parent and len(card.parent.select(LINK_SEL)) == 1:
            card = card.parent

        h2 = a.find_parent("h2")
        occupation = None
        nxt = h2.find_next("p", class_="aksel-typo--semibold")
        if nxt and card in nxt.parents:
            occupation = nxt.get_text(strip=True)

        card_text = card.get_text(" ", strip=True)
        deadline = None
        for p in card.find_all("p"):
            t = p.get_text(" ", strip=True)
            if t.lower().startswith(("søk senest", "søknadsfrist")):
                deadline = t
                break

        jobs.append({
            "source": "nav",
            "id": href.rsplit("/", 1)[-1],
            "title": a.get_text(strip=True),
            "url": BASE + href,
            "is_new_today": bool(re.search(r"\bNy i dag\b", card_text)),
            "occupation": occupation,
            "employer": _after_hidden_label(card, "Arbeidsgiver"),
            "location": _after_hidden_label(card, "Sted"),
            "tags": [s.get_text(strip=True) for s in card.select("span.aksel-tag")],
            "deadline": deadline,
        })
    return jobs


# ---------------------------------------------------------------- FINN (фаза 2)
FINN_BASE = "https://www.finn.no"
FINN_CARD_SEL = "article.job-card-article__card"
FINN_LOCATIONS = [
    "2.20001.20009.20150", "2.20001.20009.20151", "2.20001.20009.22106",
    "2.20001.20009.20153", "2.20001.20009.20148", "2.20001.20009.20146",
    "2.20001.20009.20158", "2.20001.20009.20149", "2.20001.20009.20147",
]


def finn_url(page: int = 1) -> str:
    params = [("extent", "3942")] + [("location", l) for l in FINN_LOCATIONS]
    if page > 1:
        params.append(("page", str(page)))
    return f"{FINN_BASE}/job/search?{urlencode(params)}"


def parse_finn_cards(html: str):
    soup = BeautifulSoup(html, "html.parser")
    jobs, seen = [], set()
    for card in soup.select(FINN_CARD_SEL):
        link = card.select_one("a.job-card-link")
        if not link:
            continue
        job_id = card.get("id") or link["href"].rstrip("/").rsplit("/", 1)[-1]
        if job_id in seen:
            continue
        seen.add(job_id)

        employer = card.select_one(".text-caption strong")
        subtitle = card.select_one("h3")
        pills = card.select("ul.job-card__pills li")
        time_tag = card.select_one("time")
        badges = [b.get_text(strip=True) for b in card.select("w-badge")]
        href = link["href"]

        jobs.append({
            "source": "finn",
            "id": job_id,
            "title": link.get_text(strip=True),
            "url": href if href.startswith("http") else FINN_BASE + href,
            "is_new_today": False,
            "occupation": None,
            "employer": employer.get_text(strip=True) if employer else None,
            "location": pills[0].get_text(" ", strip=True) if pills else None,
            "tags": badges,
            "deadline": None,
            "description": subtitle.get_text(" ", strip=True) if subtitle else None,
            "published": time_tag.get("datetime") if time_tag else None,
            "published_text": time_tag.get_text(strip=True) if time_tag else None,
            "sponsored": any("betalt" in b.lower() for b in badges),
        })
    return jobs


SOURCES = {
    "nav":  (search_url, LINK_SEL, parse_cards),
    "finn": (finn_url, FINN_CARD_SEL, parse_finn_cards),
}


def fetch_pages(source: str, pages: int):
    from playwright.sync_api import sync_playwright

    make_url, wait_sel, parser = SOURCES[source]
    all_jobs = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        for n in range(1, pages + 1):
            page.goto(make_url(n), wait_until="networkidle")
            try:
                page.wait_for_selector(wait_sel, timeout=15000)
            except Exception:
                print(f"[{source}] страница {n}: вакансий нет, остановка")
                break
            found = parser(page.content())
            new = [j for j in found if j["id"] not in all_jobs]
            for j in new:
                all_jobs[j["id"]] = j
            print(f"[{source}] страница {n}: {len(found)} карточек, новых {len(new)}")
            if not new:
                break
        browser.close()
    return list(all_jobs.values())


def save(jobs, out):
    with open(f"{out}.json", "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)
    if jobs:
        fields = list(dict.fromkeys(k for j in jobs for k in j))
        with open(f"{out}.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for j in jobs:
                w.writerow({**j, "tags": "; ".join(j.get("tags", []))})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["nav", "finn", "all"], default="all")
    ap.add_argument("--pages", type=int, default=3, help="страниц на каждый источник")
    ap.add_argument("--html", help="файл с готовым HTML вместо загрузки сайта")
    ap.add_argument("--html-source", choices=["nav", "finn"], default="nav")
    ap.add_argument("--out", default="jobs")
    args = ap.parse_args()

    if args.html:
        with open(args.html, encoding="utf-8") as f:
            jobs = SOURCES[args.html_source][2](f.read())
    else:
        jobs = []
        for src in (["nav", "finn"] if args.source == "all" else [args.source]):
            jobs += fetch_pages(src, args.pages)

    save(jobs, args.out)
    print(f"Сохранено вакансий: {len(jobs)}")
    for j in jobs[:5]:
        print("-", f"[{j['source']}]", j["title"], "|", j["employer"], "|", j["location"])