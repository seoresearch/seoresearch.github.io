#!/usr/bin/env python3
"""
Update data/publications.json from Crossref and, when possible, download a
representative image from each new DOI landing page.

Image behavior
--------------
For records whose "image" field is blank, the script follows the DOI landing
page and looks for article-specific image metadata such as:
- citation_image
- og:image
- twitter:image
- HTML images labelled as graphical abstract / TOC / abstract graphic

If a suitable public image is found, it is downloaded to:
    img/publications/auto/

The image step is best-effort. Failure to find or download an image never makes
the publication update fail.

Manually curated records, cover links, and the separate patents array are
preserved.
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
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "publications.json"
PUBLICATIONS_HTML_PATH = ROOT / "publications.html"
IMAGE_DIR = ROOT / "img" / "publications" / "auto"

ORCID = os.environ.get("PUBLICATIONS_ORCID", "0000-0002-9569-1902")
CONTACT_EMAIL = os.environ.get(
    "CROSSREF_CONTACT_EMAIL",
    "hyowon.seo@stonybrook.edu",
)

USER_AGENT = (
    "Mozilla/5.0 (compatible; SeoResearchGroupWebsite/1.0; "
    f"+mailto:{CONTACT_EMAIL})"
)

MAX_HTML_BYTES = 6_000_000
MAX_IMAGE_BYTES = 12_000_000

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


def venue_string(
    item: dict[str, Any],
    year: int,
    publication_type: str = "",
    doi: str = "",
) -> str:
    journal = clean_text(item.get("container-title"))
    volume = clean_text(item.get("volume"))
    issue = clean_text(item.get("issue"))
    page = clean_text(item.get("page"))
    item_url = clean_text(item.get("URL")).lower()
    doi_lower = doi.lower()

    # Crossref sometimes leaves the container title blank for ChemRxiv.
    if publication_type == "preprint" and (
        "chemrxiv" in doi_lower
        or "chemrxiv" in item_url
        or "chemrxiv" in journal.lower()
    ):
        return f"ChemRxiv ({year})"

    venue = journal or ("Preprint" if publication_type == "preprint" else "Publication")
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



def is_german_angewandte_doi(doi: str) -> bool:
    """
    Wiley registers the German-language Angewandte Chemie version separately
    with a DOI beginning 10.1002/ange., while the citable International Edition
    uses 10.1002/anie.  The website keeps only the International Edition.
    """
    return doi.strip().lower().startswith("10.1002/ange.")


def remove_auto_german_angewandte_duplicates(
    records: list[dict[str, Any]],
) -> int:
    """
    Remove Crossref-added German-edition duplicates already present in the JSON.
    Manually curated records are never deleted.
    """
    kept: list[dict[str, Any]] = []
    removed = 0

    for record in records:
        doi = str(record.get("doi", "")).strip().lower()
        is_auto = (
            record.get("manual") is False
            or str(record.get("source", "")).lower() == "crossref"
        )

        if is_auto and is_german_angewandte_doi(doi):
            removed += 1
            print(
                "Removed German Angewandte Chemie duplicate: "
                f"{record.get('title', doi)} ({doi})"
            )
            continue

        kept.append(record)

    if removed:
        records[:] = kept

    return removed



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
            "User-Agent": USER_AGENT,
        },
    )

    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)

    return payload.get("message", {}).get("items", [])


class ArticleImageParser(HTMLParser):
    """Collect image candidates from article landing-page HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.candidates: list[tuple[int, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {
            (key or "").lower(): value or ""
            for key, value in attrs
        }

        if tag.lower() == "meta":
            key = (
                attrs_dict.get("property")
                or attrs_dict.get("name")
                or ""
            ).lower()
            content = attrs_dict.get("content", "").strip()
            if not content:
                return

            priorities = {
                "citation_image": 150,
                "citation_graphical_abstract": 150,
                "og:image": 110,
                "og:image:url": 110,
                "twitter:image": 100,
                "twitter:image:src": 100,
            }

            if key in priorities:
                self.candidates.append((priorities[key], content, key))

        elif tag.lower() == "img":
            src = (
                attrs_dict.get("src")
                or attrs_dict.get("data-src")
                or attrs_dict.get("data-lazy-src")
                or ""
            ).strip()

            if not src:
                return

            label = " ".join(
                [
                    attrs_dict.get("alt", ""),
                    attrs_dict.get("title", ""),
                    attrs_dict.get("class", ""),
                    src,
                ]
            ).lower()

            score = 15
            if "graphical abstract" in label:
                score += 150
            elif "graphical" in label:
                score += 120
            elif "toc graphic" in label or "toc image" in label:
                score += 120
            elif "abstract graphic" in label or "abstract image" in label:
                score += 110
            elif "article image" in label or "article figure" in label:
                score += 70
            elif "figure" in label:
                score += 35

            self.candidates.append((score, src, label))


def fetch_html(url: str) -> tuple[str, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
    )

    with urllib.request.urlopen(request, timeout=45) as response:
        final_url = response.geturl()
        raw = response.read(MAX_HTML_BYTES + 1)

        if len(raw) > MAX_HTML_BYTES:
            raw = raw[:MAX_HTML_BYTES]

        charset = response.headers.get_content_charset() or "utf-8"
        text = raw.decode(charset, errors="replace")

    return final_url, text


def is_bad_image_candidate(url: str, label: str) -> bool:
    text = f"{url} {label}".lower()

    blocked = (
        "favicon",
        "logo",
        "brandmark",
        "avatar",
        "profile",
        "sprite",
        "tracking",
        "pixel.gif",
        "spacer",
        "icon-",
        "/icon/",
    )

    return (
        not url
        or url.startswith("data:")
        or any(token in text for token in blocked)
    )


def image_candidates_for_doi(doi: str) -> list[tuple[int, str, str, str]]:
    doi_url = f"https://doi.org/{urllib.parse.quote(doi, safe='/:')}"
    final_url, page_html = fetch_html(doi_url)

    parser = ArticleImageParser()
    parser.feed(page_html)

    candidates: list[tuple[int, str, str, str]] = []

    for score, candidate, label in parser.candidates:
        absolute = urllib.parse.urljoin(final_url, candidate)

        if is_bad_image_candidate(absolute, label):
            continue

        candidates.append((score, absolute, label, final_url))

    # Deduplicate while keeping the highest-scoring version.
    best_by_url: dict[str, tuple[int, str, str, str]] = {}
    for item in candidates:
        score, url, label, referer = item
        previous = best_by_url.get(url)
        if previous is None or score > previous[0]:
            best_by_url[url] = item

    return sorted(
        best_by_url.values(),
        key=lambda item: item[0],
        reverse=True,
    )


def extension_for_content_type(content_type: str, image_url: str) -> str:
    content_type = content_type.split(";", 1)[0].strip().lower()

    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/svg+xml": ".svg",
    }

    if content_type in mapping:
        return mapping[content_type]

    suffix = Path(urllib.parse.urlparse(image_url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}:
        return ".jpg" if suffix == ".jpeg" else suffix

    return ".jpg"


def doi_filename(doi: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", doi).strip("_")
    return stem[:160] or "publication"


def download_image(
    image_url: str,
    referer: str,
    doi: str,
) -> Path | None:
    request = urllib.request.Request(
        image_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Referer": referer,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            content_type = response.headers.get("Content-Type", "")

            if not content_type.lower().startswith("image/"):
                return None

            chunks: list[bytes] = []
            total = 0

            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break

                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    return None

                chunks.append(chunk)

            body = b"".join(chunks)

    except Exception:
        return None

    if len(body) < 2_000:
        return None

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    extension = extension_for_content_type(content_type, image_url)
    path = IMAGE_DIR / f"{doi_filename(doi)}{extension}"
    path.write_bytes(body)

    return path


def fetch_representative_image(doi: str) -> str:
    """
    Return a repository-relative image path, or an empty string on failure.
    """
    try:
        candidates = image_candidates_for_doi(doi)
    except Exception as exc:
        print(f"Image lookup failed for {doi}: {exc}")
        return ""

    for score, image_url, label, referer in candidates[:12]:
        path = download_image(image_url, referer, doi)

        if path is None:
            continue

        relative = path.relative_to(ROOT).as_posix()
        print(
            f"Fetched representative image for {doi}: "
            f"{relative} (candidate score {score})"
        )
        return f"./{relative}"

    print(f"No usable representative image found for {doi}.")
    return ""


def add_images_to_blank_records(records: list[dict[str, Any]]) -> int:
    """
    Backfill blank image fields. This lets an already-added publication acquire
    an image on the next workflow run without deleting/re-adding the record.
    """
    count = 0

    for record in records:
        doi = str(record.get("doi", "")).strip()
        current_image = str(record.get("image", "")).strip()

        if not doi or current_image:
            continue

        image = fetch_representative_image(doi)
        if not image:
            continue

        record["image"] = image
        record["image_alt"] = (
            "Representative image for " +
            str(record.get("title", "publication"))
        )
        count += 1

    return count



def sync_inline_publication_data(data: dict[str, Any]) -> bool:
    """
    Embed the current publication database directly into publications.html.

    This avoids runtime fetch/CORS/cache/path problems on GitHub Pages.
    data/publications.json remains the source of truth used by this updater.
    """
    if not PUBLICATIONS_HTML_PATH.exists():
        raise FileNotFoundError(
            f"Could not find publications page: {PUBLICATIONS_HTML_PATH}"
        )

    page = PUBLICATIONS_HTML_PATH.read_text(encoding="utf-8")

    start_marker = "/* SEO_PUBLICATION_DATA_START */"
    end_marker = "/* SEO_PUBLICATION_DATA_END */"

    start = page.find(start_marker)
    end = page.find(end_marker)

    if start == -1 or end == -1 or end <= start:
        raise RuntimeError(
            "publications.html is missing the inline publication-data markers."
        )

    serialized = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")

    replacement = (
        start_marker
        + "\n    window.SEO_PUBLICATIONS_DATA = "
        + serialized
        + ";\n    "
        + end_marker
    )

    old_block = page[start:end + len(end_marker)]
    if old_block == replacement:
        return False

    updated_page = (
        page[:start]
        + replacement
        + page[end + len(end_marker):]
    )

    PUBLICATIONS_HTML_PATH.write_text(updated_page, encoding="utf-8")
    print("Updated inline publication data in publications.html.")
    return True




def load_publication_data() -> tuple[dict[str, Any], bool]:
    """
    Load data/publications.json.

    If the JSON file is malformed, recover from the valid publication data
    embedded in publications.html, then let the normal updater rewrite a clean
    JSON file. This makes the scheduled workflow self-healing after an
    accidental manual JSON syntax error.
    """
    if not DATA_PATH.exists():
        raise FileNotFoundError(
            f"Could not find publication database: {DATA_PATH}"
        )

    raw = DATA_PATH.read_text(encoding="utf-8")

    try:
        return json.loads(raw), False
    except json.JSONDecodeError as exc:
        print(
            "WARNING: data/publications.json is invalid JSON. "
            f"{exc}. Attempting recovery from publications.html."
        )

    if not PUBLICATIONS_HTML_PATH.exists():
        raise RuntimeError(
            "Could not recover publication data because publications.html "
            "is missing."
        )

    page = PUBLICATIONS_HTML_PATH.read_text(encoding="utf-8")

    start_marker = "/* SEO_PUBLICATION_DATA_START */"
    end_marker = "/* SEO_PUBLICATION_DATA_END */"

    start = page.find(start_marker)
    end = page.find(end_marker)

    if start == -1 or end == -1 or end <= start:
        raise RuntimeError(
            "Could not recover publication data because publications.html "
            "does not contain the inline publication-data markers."
        )

    block = page[start + len(start_marker):end]

    match = re.search(
        r"window\.SEO_PUBLICATIONS_DATA\s*=\s*(\{.*\})\s*;\s*$",
        block,
        flags=re.DOTALL,
    )

    if not match:
        raise RuntimeError(
            "Could not recover publication data from the inline data block "
            "in publications.html."
        )

    recovered = json.loads(match.group(1))

    if not isinstance(recovered, dict) or not isinstance(
        recovered.get("publications"), list
    ):
        raise RuntimeError(
            "Recovered publication data are missing the publications array."
        )

    print(
        "Recovered publication data from publications.html. "
        "A clean data/publications.json will be written."
    )

    return recovered, True



def main() -> int:
    data, json_recovered = load_publication_data()
    records = data.setdefault("publications", [])

    # Remove German Angewandte Chemie mirror records that an earlier
    # Crossref run may have added as separate publications.
    duplicates_removed = remove_auto_german_angewandte_duplicates(records)

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

        # Angewandte Chemie has parallel German (ange) and International
        # Edition (anie) records. Keep only the International Edition.
        if is_german_angewandte_doi(doi):
            print(f"Skipping German Angewandte Chemie record: {doi}")
            continue

        parts = date_parts(item)
        year = parts[0] if parts else date.today().year
        publication_type, badge = classify(item)

        record = {
            "title": clean_text(item.get("title")),
            "authors": format_authors(item.get("author", [])),
            "venue": venue_string(item, year, publication_type, doi),
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
                    "label": (
                        "Preprint"
                        if publication_type == "preprint"
                        else "Article"
                    ),
                    "url": f"https://doi.org/{doi}",
                }
            ],
            "manual": False,
            "source": "Crossref",
        }

        records.append(record)
        existing_dois.add(doi)
        added.append(doi)

    # Also backfill images for publications that were added on an earlier run
    # with a blank "image" field.
    images_added = add_images_to_blank_records(records)

    metadata_changed = bool(
        added or images_added or duplicates_removed or json_recovered
    )

    records.sort(
        key=lambda record: (
            int(record.get("year", 0)),
            str(record.get("sort_date", "")),
            str(record.get("title", "")),
        ),
        reverse=True,
    )

    if metadata_changed:
        data["last_updated"] = str(date.today())

        DATA_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    html_changed = sync_inline_publication_data(data)

    if not metadata_changed and not html_changed:
        print(
            "No new Crossref records, representative images, "
            "duplicate corrections, or website-data changes were found."
        )
        return 0

    if added:
        print(f"Added {len(added)} publication(s):")
        for doi in added:
            print(f"  - {doi}")

    if images_added:
        print(f"Added {images_added} representative image(s).")

    if duplicates_removed:
        print(
            f"Removed {duplicates_removed} German Angewandte Chemie "
            "duplicate record(s)."
        )

    if json_recovered:
        print("Repaired malformed data/publications.json.")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Publication update failed: {exc}", file=sys.stderr)
        raise
