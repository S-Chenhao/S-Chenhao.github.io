#!/usr/bin/env python3
"""Sync Scholar through SerpApi; read the key only from SERPAPI_API_KEY.

API schema: https://serpapi.com/google-scholar-author-api
No API response, credential, or credential-bearing URL is written to logs/files.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from sync_scholar import SyncError, pending_publications, write_outputs

ENDPOINT = "https://serpapi.com/search.json"
PAGE_SIZE = 100
MAX_BYTES = 5_000_000


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SyncError("SerpApi unexpectedly redirected the request")


def fetch_page(scholar_id: str, start: int, api_key: str, deadline: float) -> dict:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SyncError("SerpApi synchronization exceeded its time limit")
    url = ENDPOINT + "?" + urlencode({
        "engine": "google_scholar_author", "author_id": scholar_id,
        "hl": "en", "start": start, "num": PAGE_SIZE, "api_key": api_key,
    })
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with build_opener(NoRedirect()).open(request, timeout=min(45, remaining)) as response:
            body = response.read(MAX_BYTES + 1)
    except HTTPError as error:
        if error.code in (401, 403):
            message = "SerpApi rejected the API key; check SERPAPI_API_KEY and account access"
        elif error.code == 429:
            message = "SerpApi quota or rate limit reached; previous data retained"
        else:
            message = f"SerpApi HTTP {error.code}; previous data retained"
        # Do not include the exception, URL, or upstream body: they may contain the key.
        raise SyncError(message) from None
    except (URLError, TimeoutError, OSError):
        raise SyncError("SerpApi network request failed; previous data retained") from None
    if len(body) > MAX_BYTES:
        raise SyncError("SerpApi response exceeded the size limit")
    try:
        return json.loads(body)
    except (ValueError, UnicodeError):
        raise SyncError("SerpApi returned invalid JSON") from None


def object_value(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise SyncError(f"Missing or invalid SerpApi {label}")
    return value


def count(value, label: str) -> int:
    if type(value) is not int or value < 0:
        raise SyncError(f"Missing or invalid SerpApi {label}")
    return value


def parse_page(payload, scholar_id: str, start: int) -> tuple[tuple[int, int], list[dict], bool]:
    payload = object_value(payload, "response")
    metadata = object_value(payload.get("search_metadata"), "metadata")
    if payload.get("error") or metadata.get("status") != "Success":
        raise SyncError("SerpApi did not return a successful result; check its dashboard")
    parameters = object_value(payload.get("search_parameters"), "search parameters")
    if parameters.get("author_id") != scholar_id or parameters.get("engine") != "google_scholar_author":
        raise SyncError("SerpApi returned a different author or search engine")
    offset = parameters.get("start", parameters.get("cstart", 0))
    if str(offset) != str(start):
        raise SyncError("SerpApi returned an unexpected page offset")
    author = object_value(payload.get("author"), "author")
    if not isinstance(author.get("name"), str) or not author["name"].strip():
        raise SyncError("SerpApi author name is missing")

    table = object_value(payload.get("cited_by"), "citation metrics").get("table")
    if not isinstance(table, list):
        raise SyncError("SerpApi citation metrics table is missing")
    metrics = {}
    for row in table:
        row = object_value(row, "metric row")
        for key in ("citations", "h_index"):
            if key in row:
                if key in metrics:
                    raise SyncError("SerpApi returned duplicate metrics")
                metrics[key] = count(object_value(row[key], key).get("all"), key)
    if set(metrics) != {"citations", "h_index"}:
        raise SyncError("SerpApi all-time citations or h-index is missing")

    articles = payload.get("articles")
    if not isinstance(articles, list) or len(articles) > PAGE_SIZE:
        raise SyncError("SerpApi article list is missing or too large")
    papers = []
    for article in articles:
        article = object_value(article, "article")
        citation_id, title = article.get("citation_id"), article.get("title")
        if (not isinstance(citation_id, str)
                or not re.fullmatch(re.escape(scholar_id) + r":[A-Za-z0-9_-]+", citation_id)):
            raise SyncError("SerpApi article identifier is missing or belongs to another author")
        if not isinstance(title, str) or not title.strip():
            raise SyncError("SerpApi article title is missing")
        paper = {
            "scholarId": citation_id, "title": " ".join(title.split()),
            "citations": count(object_value(article.get("cited_by"), "article citations").get("value"), "article citations"),
            "url": "https://scholar.google.com/citations?" + urlencode({
                "view_op": "view_citation", "hl": "en", "user": scholar_id,
                "citation_for_view": citation_id,
            }),
        }
        year = article.get("year")
        if year not in (None, ""):
            if not re.fullmatch(r"[12][0-9]{3}", str(year)):
                raise SyncError("SerpApi article year is invalid")
            paper["year"] = int(year)
        papers.append(paper)

    pagination = object_value(payload.get("serpapi_pagination", {}), "pagination")
    next_url = pagination.get("next")
    has_more = next_url is not None
    if has_more:
        if not isinstance(next_url, str):
            raise SyncError("SerpApi next page is invalid")
        parsed = urlparse(next_url)
        query = parse_qs(parsed.query)
        offsets = query.get("start", query.get("cstart", []))
        # Only inspect pagination metadata; never follow a response-provided URL.
        if (parsed.scheme != "https" or parsed.hostname != "serpapi.com"
                or query.get("author_id") != [scholar_id]
                or query.get("engine") != ["google_scholar_author"]
                or offsets != [str(start + len(papers))] or not papers):
            raise SyncError("SerpApi pagination does not match the received articles")
    if not papers and (start or metrics["citations"] or metrics["h_index"]):
        raise SyncError("SerpApi returned an inconsistent empty article list")
    return (metrics["citations"], metrics["h_index"]), papers, has_more


def collect_profile(scholar_id: str, *, api_key: str = "", fetch=None, max_pages: int = 20) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", scholar_id):
        raise SyncError("Invalid Scholar profile identifier")
    if fetch is None:
        if not api_key.strip():
            raise SyncError("Add the repository Actions secret SERPAPI_API_KEY before running")
        deadline = time.monotonic() + 120
        fetch = lambda start: fetch_page(scholar_id, start, api_key.strip(), deadline)
    papers, seen, previous_metrics = [], set(), None
    for _ in range(max_pages):
        metrics, page, has_more = parse_page(fetch(len(papers)), scholar_id, len(papers))
        if previous_metrics is not None and metrics != previous_metrics:
            raise SyncError("SerpApi metrics changed during pagination; previous data retained")
        previous_metrics = metrics
        for paper in page:
            if paper["scholarId"] in seen:
                raise SyncError("SerpApi returned repeated articles across pages")
            seen.add(paper["scholarId"])
            papers.append(paper)
        if not has_more:
            if metrics[1] > len(papers) or metrics[0] < metrics[1] ** 2:
                raise SyncError("SerpApi metrics are inconsistent with the publication record")
            return {
                "schemaVersion": 1, "scholarId": scholar_id, "source": "serpapi",
                "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "totalCitations": metrics[0], "hIndex": metrics[1], "publications": papers,
            }
    raise SyncError("SerpApi pagination limit exceeded; previous data retained")


def sync(scholar_id: str, output: Path, curated: Path, pending: Path, *, api_key="", fetch=None) -> dict:
    if len({output.resolve(), curated.resolve(), pending.resolve()}) != 3:
        raise SyncError("Snapshot, curated, and pending paths must be distinct")
    curated_data = json.loads(curated.read_text(encoding="utf-8"))
    snapshot = collect_profile(scholar_id, api_key=api_key, fetch=fetch)
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
        snapshot = sync(args.scholar_id, args.output, args.curated, args.pending,
                        api_key=os.environ.get("SERPAPI_API_KEY", ""))
    except SyncError as error:
        print(f"Scholar API sync failed; previous data retained: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("Scholar API sync failed while reading or saving data; previous data retained", file=sys.stderr)
        return 1
    print(f"Scholar API sync succeeded: {snapshot['totalCitations']} citations, "
          f"h-index {snapshot['hIndex']}, {len(snapshot['publications'])} publications")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
