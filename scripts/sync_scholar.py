#!/usr/bin/env python3
"""Refresh public Google Scholar data without overwriting the last good snapshot.

Uses Python's standard library. An unsuccessful run exits nonzero and leaves both
JSON outputs unchanged. New, uncurated papers are recorded for review separately.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen


BASE_URL = "https://scholar.google.com/citations"
PAGE_SIZE = 100
MAX_RESPONSE_BYTES = 5_000_000
VOID_TAGS = frozenset("area base br col embed hr img input link meta param source track wbr".split())


class SyncError(Exception):
    """A response or operation could not safely produce a complete snapshot."""


@dataclass
class Element:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list[Element | str] = field(default_factory=list)

    def text(self) -> str:
        return "".join(child.text() if isinstance(child, Element) else child for child in self.children)

    def find_all(self, *, tag=None, id=None, class_name=None) -> list[Element]:
        result = []
        for child in self.children:
            if not isinstance(child, Element):
                continue
            if ((tag is None or child.tag == tag)
                    and (id is None or child.attrs.get("id") == id)
                    and (class_name is None or class_name in (child.attrs.get("class") or "").split())):
                result.append(child)
            result.extend(child.find_all(tag=tag, id=id, class_name=class_name))
        return result


class ScholarHTML(HTMLParser):
    """Small DOM sufficient for Scholar's server-rendered profile tables."""

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.root = Element("document")
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Element(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def one(root: Element, description: str, **selector) -> Element:
    matches = root.find_all(**selector)
    if len(matches) != 1:
        raise SyncError(f"Expected exactly one {description}; profile may be blocked or incomplete")
    return matches[0]


def integer(text: str, description: str, *, allow_empty=False) -> int:
    text = text.strip()
    if not text and allow_empty:
        return 0
    if not re.fullmatch(r"(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)", text):
        raise SyncError(f"Invalid {description}: {text!r}")
    return int(text.replace(",", ""))


@dataclass
class ProfilePage:
    total_citations: int
    h_index: int
    publications: list[dict]
    has_more: bool


def parse_page(html: str, scholar_id: str, *, start: int = 0) -> ProfilePage:
    root = ScholarHTML(html).root
    name = one(root, "profile name", id="gsc_prf_in")
    if not name.text().strip():
        raise SyncError("Profile name is empty")
    stats = one(root, "citation statistics table", id="gsc_rsb_st")
    metrics = {}
    for row in stats.find_all(tag="tr"):
        labels = row.find_all(class_name="gsc_rsb_sc1")
        values = row.find_all(class_name="gsc_rsb_std")
        if labels and values:
            label = " ".join(labels[0].text().split()).casefold()
            if label in ("citations", "h-index"):
                if label in metrics:
                    raise SyncError(f"Duplicate {label} metric")
                metrics[label] = integer(values[0].text(), label)
    if set(metrics) != {"citations", "h-index"}:
        raise SyncError("All-time citation count or h-index is missing")

    articles = one(root, "publication table", id="gsc_a_b")
    publications = []
    for row in articles.find_all(tag="tr", class_name="gsc_a_tr"):
        title = one(row, "publication title", tag="a", class_name="gsc_a_at")
        title_text = " ".join(title.text().split())
        if not title_text:
            raise SyncError("Publication title is empty")
        href = urlparse(urljoin(BASE_URL, title.attrs.get("href") or ""))
        query = parse_qs(href.query)
        citation_ids = query.get("citation_for_view", [])
        if (href.scheme != "https" or href.hostname != "scholar.google.com"
                or len(citation_ids) != 1
                or not re.fullmatch(re.escape(scholar_id) + r":[A-Za-z0-9_-]+", citation_ids[0])):
            raise SyncError("Publication identifier is missing or belongs to another profile")
        citation_cell = one(row, "publication citation cell", tag="td", class_name="gsc_a_c")
        citation_link = one(citation_cell, "publication citation link", tag="a", class_name="gsc_a_ac")
        publication = {
            "scholarId": citation_ids[0],
            "title": title_text,
            # Scholar intentionally leaves this link empty for uncited papers.
            "citations": integer(citation_link.text(), "publication citations", allow_empty=True),
            "url": BASE_URL + "?" + urlencode({
                "view_op": "view_citation", "hl": "en", "user": scholar_id,
                "citation_for_view": citation_ids[0],
            }),
        }
        year_cell = one(row, "publication year cell", tag="td", class_name="gsc_a_y")
        year = year_cell.text().strip()
        if year:
            if not re.fullmatch(r"[12][0-9]{3}", year):
                raise SyncError(f"Invalid publication year: {year!r}")
            publication["year"] = int(year)
        publications.append(publication)

    more = one(root, "Show more control", id="gsc_bpf_more")
    has_more = "disabled" not in more.attrs and more.attrs.get("aria-disabled") != "true"
    if len(publications) > PAGE_SIZE or (has_more and len(publications) != PAGE_SIZE):
        raise SyncError("Publication page is incomplete or has an unexpected size")
    if not publications:
        empty_markers = articles.find_all(class_name="gsc_a_e")
        if (start or has_more or metrics["citations"] or metrics["h-index"]
                or not any("no articles" in marker.text().casefold() for marker in empty_markers)):
            raise SyncError("Empty publication table is not an explicit empty profile")
    # The footer is a presentation hint, not a required pagination field: it can
    # be empty or include labels/directional marks in server-rendered HTML.
    # Keep checking explicit numeric ranges, in addition to the required row,
    # Show more, profile, metric, and cross-page checks above/below.
    ranges = root.find_all(id="gsc_a_nn")
    if len(ranges) > 1:
        raise SyncError("Duplicate publication range")
    if ranges and publications:
        range_text = "".join(char for char in ranges[0].text()
                             if unicodedata.category(char) != "Cf").strip()
        visible_range = re.search(r"(?<![0-9,])([0-9][0-9,]*)\s*[–—-]\s*([0-9][0-9,]*)(?![0-9,])", range_text)
        if visible_range:
            received = (integer(visible_range[1], "range start"),
                        integer(visible_range[2], "range end"))
            expected = (start + 1, start + len(publications))
            if received != expected:
                raise SyncError(f"Publication range does not match the rows received: "
                                f"label={range_text!r}, expected={expected}, rows={len(publications)}")
        elif range_text:
            print(f"Scholar footer has no numeric range: {range_text!r}; "
                  "validated publication rows and Show more control instead", file=sys.stderr)
    return ProfilePage(metrics["citations"], metrics["h-index"], publications, has_more)


def fetch_html(url: str, *, deadline: float, timeout: float = 20, attempts: int = 3) -> str:
    for attempt in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SyncError("Scholar sync exceeded its overall time limit")
        request = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; AcademicHomepageSync/1.0)",
            "Accept": "text/html", "Accept-Language": "en-US,en;q=0.9",
        })
        try:
            with urlopen(request, timeout=min(timeout, remaining)) as response:
                location = urlparse(response.geturl())
                if (location.hostname != "scholar.google.com" or location.path != "/citations"
                        or parse_qs(location.query).get("user") != parse_qs(urlparse(url).query).get("user")):
                    raise SyncError("Scholar redirected away from the public profile")
                if response.headers.get_content_type() != "text/html":
                    raise SyncError("Scholar returned a non-HTML response")
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise SyncError("Scholar response exceeded the size limit")
                return body.decode(response.headers.get_content_charset() or "utf-8")
        except HTTPError as error:
            # Do not try to circumvent access controls or CAPTCHA challenges.
            if error.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise SyncError(f"Scholar HTTP {error.code}; previous data retained") from error
        except (URLError, TimeoutError, OSError) as error:
            if attempt == attempts - 1:
                raise SyncError(f"Scholar request failed: {error}") from error
        time.sleep(min(2 ** attempt, max(0, deadline - time.monotonic())))
    raise SyncError("Scholar request failed")


def collect_profile(scholar_id: str, *, fetch=None, max_pages: int = 20) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", scholar_id):
        raise SyncError("Invalid Scholar profile identifier")
    if fetch is None:
        deadline = time.monotonic() + 120
        fetch = lambda url: fetch_html(url, deadline=deadline)
    publications = []
    seen = set()
    metrics = None
    for _ in range(max_pages):
        start = len(publications)
        url = BASE_URL + "?" + urlencode({"user": scholar_id, "hl": "en", "cstart": start, "pagesize": PAGE_SIZE})
        page = parse_page(fetch(url), scholar_id, start=start)
        page_metrics = (page.total_citations, page.h_index)
        if metrics is not None and metrics != page_metrics:
            raise SyncError("Scholar metrics changed during pagination; retry on the next run")
        metrics = page_metrics
        for publication in page.publications:
            if publication["scholarId"] in seen:
                raise SyncError("Repeated publication detected; pagination may be incomplete")
            seen.add(publication["scholarId"])
            publications.append(publication)
        if not page.has_more:
            if page.h_index > len(publications) or page.total_citations < page.h_index ** 2:
                raise SyncError("Profile metrics are inconsistent with its publication record")
            return {
                "schemaVersion": 1, "scholarId": scholar_id,
                "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "totalCitations": page.total_citations, "hIndex": page.h_index,
                "publications": publications,
            }
    raise SyncError(f"Profile exceeded the pagination limit ({max_pages} pages); previous data retained")


def normalized_title(title: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", title).casefold() if char.isalnum())


def pending_publications(snapshot: dict, curated: object) -> dict:
    if not isinstance(curated, list):
        raise SyncError("Curated publications JSON must contain an array")
    ids, titles = set(), set()
    for entry in curated:
        if not isinstance(entry, dict) or not isinstance(entry.get("title"), str) or not normalized_title(entry["title"]):
            raise SyncError("Every curated publication needs a nonempty title")
        if entry.get("scholarId") is not None:
            if not isinstance(entry["scholarId"], str) or not entry["scholarId"].startswith(snapshot["scholarId"] + ":"):
                raise SyncError("Curated Scholar identifier belongs to another profile")
            ids.add(entry["scholarId"])
        titles.add(normalized_title(entry["title"]))
    return {
        "schemaVersion": 1, "scholarId": snapshot["scholarId"], "updatedAt": snapshot["updatedAt"],
        "publications": [entry for entry in snapshot["publications"]
                         if entry["scholarId"] not in ids and normalized_title(entry["title"]) not in titles],
    }


def stage_bytes(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o644)
        return Path(temporary)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def write_outputs(outputs: dict[Path, dict]) -> None:
    """Stage all results first, atomically replace each, roll back on write errors."""
    staged, backups, replaced = {}, {}, []
    try:
        for path, payload in outputs.items():
            content = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
            staged[path] = stage_bytes(path, content)
            backups[path] = stage_bytes(path, path.read_bytes()) if path.exists() else None
        for path, temporary in staged.items():
            os.replace(temporary, path)
            replaced.append(path)
    except BaseException:
        for path in reversed(replaced):
            if backups[path] is None:
                path.unlink(missing_ok=True)
            else:
                os.replace(backups[path], path)
        raise
    finally:
        for temporary in (*staged.values(), *backups.values()):
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def sync(scholar_id: str, output: Path, curated: Path, pending: Path, *, fetch=None) -> dict:
    if len({output.resolve(), curated.resolve(), pending.resolve()}) != 3:
        raise SyncError("Snapshot, curated, and pending paths must be distinct")
    curated_data = json.loads(curated.read_text(encoding="utf-8"))
    snapshot = collect_profile(scholar_id, fetch=fetch)
    pending_data = pending_publications(snapshot, curated_data)
    write_outputs({output: snapshot, pending: pending_data})
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scholar-id", default="Vqf7dwEAAAAJ")
    parser.add_argument("--output", type=Path, default=Path("public/scholar.json"))
    parser.add_argument("--curated", type=Path, default=Path("data/publications.json"))
    parser.add_argument("--pending", type=Path, default=Path("data/scholar-pending.json"))
    args = parser.parse_args()
    try:
        snapshot = sync(args.scholar_id, args.output, args.curated, args.pending)
    except (SyncError, OSError, ValueError) as error:
        print(f"Scholar sync failed; last successful snapshot retained: {error}", file=sys.stderr)
        return 1
    print(f"Scholar sync succeeded: {snapshot['totalCitations']} citations, h-index {snapshot['hIndex']}, {len(snapshot['publications'])} publications")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
