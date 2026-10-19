#!/usr/bin/env python3
"""Парсер вакансий arbeidsplassen.nav.no

Установка:
    pip install beautifulsoup4 playwright
    playwright install chromium

Запуск:
    python arbeidsplassen_parser.py --pages 5 --out jobs        # -> jobs.json + jobs.csv
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


def fetch_pages(pages: int):
    from playwright.sync_api import sync_playwright

    all_jobs = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        for n in range(1, pages + 1):
            page.goto(search_url(n), wait_until="networkidle")
            try:
                page.wait_for_selector(LINK_SEL, timeout=15000)
            except Exception:
                print(f"Страница {n}: вакансий нет, остановка")
                break
            found = parse_cards(page.content())
            new = [j for j in found if j["id"] not in all_jobs]
            for j in new:
                all_jobs[j["id"]] = j
            print(f"Страница {n}: {len(found)} карточек, новых {len(new)}")
            if not new:
                break
        browser.close()
    return list(all_jobs.values())


def save(jobs, out):
    with open(f"{out}.json", "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)
    if jobs:
        with open(f"{out}.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=jobs[0].keys())
            w.writeheader()
            for j in jobs:
                w.writerow({**j, "tags": "; ".join(j["tags"])})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=3)
    ap.add_argument("--html", help="файл с готовым HTML вместо загрузки сайта")
    ap.add_argument("--out", default="jobs")
    args = ap.parse_args()

    if args.html:
        with open(args.html, encoding="utf-8") as f:
            jobs = parse_cards(f.read())
    else:
        jobs = fetch_pages(args.pages)

    save(jobs, args.out)
    print(f"Сохранено вакансий: {len(jobs)}")
    for j in jobs[:5]:
        print("-", j["title"], "|", j["employer"], "|", j["deadline"])