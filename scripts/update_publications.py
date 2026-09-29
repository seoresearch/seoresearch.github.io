#!/usr/bin/env python3
"""
Update data/publications.json with newly registered Crossref works associated
with Hyowon Seo's ORCID.

This script:
- reads the existing publication database,
- queries Crossref by ORCID,
- appends only DOI records not already present,
- preserves manually curated records and the separate patents array,
- writes changes back to data/publications.json.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "publications.json"

ORCID = os.environ.get("PUBLICATIONS_ORCID", "0000-0002-9569-1902")
CONTACT_EMAIL = os.environ.get(
    "CROSSREF_CONTACT_EMAIL",
    "hyowon.seo@stonybrook.edu",
)

CROSSREF_FIELDS = ",".join(
    [
        "DOI",
        "title",
        "author",
        "container-title",
        "published-print",
        "published-online",
        "issued",
        "volume",
        "issue",
        "page",
        "type",
        "URL",
    ]
)


def clean_text(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", value).strip()


def initials(given: str) -> str:
    parts = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", given or "")
    return " ".join(f"{part[0]}." for part in parts if part)


def format_authors(authors: list[dict[str, Any]]) -> str:
    formatted: list[str] = []
    for author in authors:
        family = clean_text(author.get("family"))
        given = clean_text(author.get("given"))
        if not family:
            continue
        initial_text = initials(given)
        formatted.append(f"{family}, {initial_text}" if initial_text else family)
    return "; ".join(formatted)


def date_parts(item: dict[str, Any]) -> list[int]:
    for key in ("published-print", "published-online", "issued"):
        parts = item.get(key, {}).get("date-parts", [[]])[0]
        if parts:
            return [int(part) for part in parts]
    return []


def iso_date(parts: list[int]) -> str:
    if not parts:
        return ""
    year = parts[0]
    month = parts[1] if len(parts) > 1 else 1
    day = parts[2] if len(parts) > 2 else 1
    return f"{year:04d}-{month:02d}-{day:02d}"


def venue_string(item: dict[str, Any], year: int) -> str:
    journal = clean_text(item.get("container-title"))
    volume = clean_text(item.get("volume"))
    issue = clean_text(item.get("issue"))
    page = clean_text(item.get("page"))

    venue = journal or "Publication"
    venue += f" {year}"
    if volume:
        venue += f", {volume}"
        if issue:
            venue += f" ({issue})"
    if page:
        venue += f", {page.replace('-', '–')}"
    return venue


def classify(item: dict[str, Any]) -> tuple[str, str]:
    crossref_type = clean_text(item.get("type")).lower()
    container = clean_text(item.get("container-title")).lower()

    if crossref_type == "posted-content" or "chemrxiv" in container:
        return "preprint", "Preprint"
    if crossref_type == "book-chapter":
        return "chapter", "Book Chapter"
    if crossref_type == "proceedings-article":
        return "conference", "Conference"
    return "article", "Article"


def fetch_crossref() -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "filter": f"orcid:{ORCID}",
            "rows": "1000",
            "select": CROSSREF_FIELDS,
            "mailto": CONTACT_EMAIL,
        }
    )
    url = f"https://api.crossref.org/works?{query}"

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": (
                "SeoResearchGroupWebsite/1.0 "
                f"(mailto:{CONTACT_EMAIL})"
            ),
        },
    )

    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    return payload.get("message", {}).get("items", [])


def main() -> int:
    if not DATA_PATH.exists():
        raise FileNotFoundError(f"Could not find publication database: {DATA_PATH}")

    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    records = data.setdefault("publications", [])

    existing_dois = {
        str(record.get("doi", "")).strip().lower()
        for record in records
        if record.get("doi")
    }

    added: list[str] = []

    for item in fetch_crossref():
        doi = clean_text(item.get("DOI")).lower()
        if not doi or doi in existing_dois:
            continue

        parts = date_parts(item)
        year = parts[0] if parts else date.today().year
        publication_type, badge = classify(item)

        record = {
            "title": clean_text(item.get("title")),
            "authors": format_authors(item.get("author", [])),
            "venue": venue_string(item, year),
            "year": year,
            "sort_date": iso_date(parts),
            "section": "current" if year >= 2024 else "prior",
            "type": publication_type,
            "badge": badge,
            "doi": doi,
            "image": "",
            "image_alt": "",
            "links": [
                {
                    "label": "Preprint" if publication_type == "preprint" else "Article",
                    "url": f"https://doi.org/{doi}",
                }
            ],
            "manual": False,
            "source": "Crossref",
        }

        records.append(record)
        existing_dois.add(doi)
        added.append(doi)

    if not added:
        print("No new Crossref records were found.")
        return 0

    records.sort(
        key=lambda record: (
            int(record.get("year", 0)),
            str(record.get("sort_date", "")),
            str(record.get("title", "")),
        ),
        reverse=True,
    )

    data["last_updated"] = str(date.today())
    DATA_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Added {len(added)} publication(s):")
    for doi in added:
        print(f"  - {doi}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Publication update failed: {exc}", file=sys.stderr)
        raise
