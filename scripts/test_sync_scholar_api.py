"""Contract and failure tests based on SerpApi's documented author JSON schema."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from sync_scholar import SyncError
from sync_scholar_api import NoRedirect, collect_profile, fetch_page, parse_page, sync

AUTHOR = "Vqf7dwEAAAAJ"


def article(index=1, citations=0):
    return {"title": f"Paper {index}", "citation_id": f"{AUTHOR}:paper_{index}",
            "cited_by": {"value": citations}, "year": "2026"}


def response(articles=None, *, start=0, citations=3, h_index=1, more=False):
    articles = [article(1, 3)] if articles is None else articles
    result = {
        "search_metadata": {"status": "Success"},
        "search_parameters": {"author_id": AUTHOR, "engine": "google_scholar_author", "start": start},
        "author": {"name": "Chenhao Si"},
        "cited_by": {"table": [{"citations": {"all": citations}}, {"h_index": {"all": h_index}}]},
        "articles": articles,
    }
    if more:
        result["serpapi_pagination"] = {"next": f"https://serpapi.com/search.json?author_id={AUTHOR}&engine=google_scholar_author&cstart={start + len(articles)}"}
    return result


class ParseTests(unittest.TestCase):
    def test_snapshot_keeps_all_time_metrics_zero_citations_and_provenance(self):
        payload = response([article(1, 3), article(2)])
        payload["articles"][1]["year"] = ""
        result = collect_profile(AUTHOR, fetch=lambda start: payload)
        self.assertEqual((result["totalCitations"], result["hIndex"], result["source"]), (3, 1, "serpapi"))
        self.assertEqual([p["citations"] for p in result["publications"]], [3, 0])
        self.assertNotIn("year", result["publications"][1])
        self.assertTrue(result["updatedAt"].endswith("Z"))
        self.assertNotIn("search_metadata", result)

    def test_wrong_author_engine_or_page_is_rejected(self):
        for key, value in [("author_id", "other"), ("engine", "google"), ("start", 100)]:
            payload = response()
            payload["search_parameters"][key] = value
            with self.subTest(key=key), self.assertRaises(SyncError):
                parse_page(payload, AUTHOR, 0)
        payload = response()
        payload["articles"][0]["citation_id"] = "other:paper_1"
        with self.assertRaises(SyncError):
            parse_page(payload, AUTHOR, 0)

    def test_missing_fields_are_not_converted_to_zero(self):
        invalid = []
        for key in ["articles", "author", "cited_by", "search_metadata"]:
            payload = response()
            del payload[key]
            invalid.append(payload)
        payload = response()
        payload["articles"][0]["cited_by"] = {}
        invalid.append(payload)
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(SyncError):
                parse_page(payload, AUTHOR, 0)

    def test_error_processing_negative_and_boolean_metrics_are_rejected(self):
        invalid = [response(citations=-1), response(citations=True), {"error": "example"}]
        processing = response()
        processing["search_metadata"]["status"] = "Processing"
        invalid.append(processing)
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(SyncError):
                parse_page(payload, AUTHOR, 0)

    def test_follows_validated_offsets_without_fetching_response_urls(self):
        offsets = []
        def fetch(start):
            offsets.append(start)
            if start == 0:
                return response([article(i) for i in range(100)], more=True)
            return response([article(100)], start=100)
        result = collect_profile(AUTHOR, fetch=fetch)
        self.assertEqual(offsets, [0, 100])
        self.assertEqual(len(result["publications"]), 101)

    def test_inconsistent_or_foreign_pagination_is_rejected(self):
        for suffix in ["https://example.com/search?start=1", f"https://serpapi.com/search.json?author_id={AUTHOR}&engine=google_scholar_author&start=200"]:
            payload = response(more=True)
            payload["serpapi_pagination"]["next"] = suffix
            with self.subTest(url=suffix), self.assertRaises(SyncError):
                parse_page(payload, AUTHOR, 0)

    def test_duplicate_articles_metric_changes_and_pagination_cap_are_rejected(self):
        for second in [response([article(1)], start=1), response([article(2)], start=1, citations=5)]:
            pages = iter([response(more=True), second])
            with self.subTest(second=second), self.assertRaises(SyncError):
                collect_profile(AUTHOR, fetch=lambda start: next(pages))
        with self.assertRaisesRegex(SyncError, "pagination limit"):
            collect_profile(AUTHOR, fetch=lambda start: response(more=True), max_pages=1)

    def test_inconsistent_empty_profile_and_h_index_are_rejected(self):
        for payload in [response([]), response(h_index=2, citations=100)]:
            with self.subTest(payload=payload), self.assertRaises(SyncError):
                collect_profile(AUTHOR, fetch=lambda start: payload)
        result = collect_profile(AUTHOR, fetch=lambda start: response([], citations=0, h_index=0))
        self.assertEqual(result["publications"], [])


class TransportTests(unittest.TestCase):
    def test_missing_key_sends_no_request(self):
        with patch("sync_scholar_api.build_opener") as opener, self.assertRaisesRegex(SyncError, "SERPAPI_API_KEY"):
            collect_profile(AUTHOR)
        opener.assert_not_called()

    def test_auth_quota_and_network_errors_never_expose_key_or_url(self):
        secret = "test-only-secret-that-must-not-appear"
        url = f"https://serpapi.com/search.json?api_key={secret}"
        for error in [HTTPError(url, 401, secret, {}, None), HTTPError(url, 429, secret, {}, None), URLError(url)]:
            with self.subTest(error=type(error).__name__), patch("sync_scholar_api.build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(SyncError) as caught:
                    fetch_page(AUTHOR, 0, secret, time.monotonic() + 120)
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn(url, str(caught.exception))
                self.assertEqual(opener.return_value.open.call_count, 1)

    def test_request_is_fixed_to_provider_and_uses_documented_parameters(self):
        with patch("sync_scholar_api.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps(response()).encode()
            fetch_page(AUTHOR, 100, "test-key", time.monotonic() + 120)
            request = opener.return_value.open.call_args.args[0]
            parsed = urlparse(request.full_url)
            query = parse_qs(parsed.query)
            self.assertEqual((parsed.scheme, parsed.netloc, parsed.path), ("https", "serpapi.com", "/search.json"))
            self.assertEqual(query["start"], ["100"])
            self.assertEqual(query["num"], ["100"])
            self.assertEqual(query["hl"], ["en"])
            self.assertLessEqual(opener.return_value.open.call_args.kwargs["timeout"], 45)

    def test_redirects_and_expired_deadline_are_rejected(self):
        with self.assertRaises(SyncError):
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com/")
        with patch("sync_scholar_api.build_opener") as opener, self.assertRaises(SyncError):
            fetch_page(AUTHOR, 0, "test-key", time.monotonic() - 1)
        opener.assert_not_called()

    def test_invalid_json_error_does_not_echo_body(self):
        with patch("sync_scholar_api.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = b"secret-body"
            with self.assertRaisesRegex(SyncError, "invalid JSON") as caught:
                fetch_page(AUTHOR, 0, "test-key", time.monotonic() + 120)
            self.assertNotIn("secret-body", str(caught.exception))


class RetentionTests(unittest.TestCase):
    def test_failure_keeps_both_files_and_success_updates_snapshot_and_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, pending, curated = (root / name for name in ["scholar.json", "pending.json", "publications.json"])
            output.write_text('{"updatedAt":null}')
            pending.write_text('{"publications":[]}')
            curated.write_text('[{"title":"Paper 1"}]')
            before = (output.read_bytes(), pending.read_bytes())
            with self.assertRaises(SyncError):
                sync(AUTHOR, output, curated, pending, fetch=lambda start: {"error": "API unavailable"})
            self.assertEqual((output.read_bytes(), pending.read_bytes()), before)
            result = sync(AUTHOR, output, curated, pending, fetch=lambda start: response([article(1, 3), article(2)]))
            self.assertEqual(json.loads(output.read_text()), result)
            self.assertEqual([p["title"] for p in json.loads(pending.read_text())["publications"]], ["Paper 2"])


if __name__ == "__main__":
    unittest.main()
