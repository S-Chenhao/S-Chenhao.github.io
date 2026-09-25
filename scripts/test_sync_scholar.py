"""Offline fixtures covering failure modes that must not erase published data."""

from html import escape
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from sync_scholar import (
    SyncError, collect_profile, fetch_html, parse_page, pending_publications, sync, write_outputs,
)


SCHOLAR_ID = "Vqf7dwEAAAAJ"


def article(index, *, title=None, citations="", year="2026"):
    title = title or f"Paper {index}"
    return f'''<tr class="gsc_a_tr">
      <td class="gsc_a_t"><a href="/citations?view_op=view_citation&amp;hl=en&amp;user={SCHOLAR_ID}&amp;citation_for_view={SCHOLAR_ID}:paper_{index}" class="gsc_a_at">{escape(title)}</a><div class="gs_gray">Author</div></td>
      <td class="gsc_a_c"><a class="gsc_a_ac gs_ibl">{citations}</a></td>
      <td class="gsc_a_y"><span class="gsc_a_h gsc_a_hc gs_ibl">{year}</span></td>
    </tr>'''


def profile(rows, *, citations="0", h_index="0", more=False, start=0):
    count = len(rows)
    visible_range = f"<span id='gsc_a_nn'>{start + 1}–{start + count}</span>" if count else ""
    rows_html = "".join(rows) if count else '<tr><td class="gsc_a_e">No articles available.</td></tr>'
    disabled = "" if more else ' disabled=""'
    return f'''<!doctype html><html><head><meta charset="utf-8"></head><body>
      <div id="gsc_prf_in">Chenhao Si</div>
      <table id="gsc_rsb_st"><thead><tr><th></th><th>All</th><th>Since 2021</th></tr></thead><tbody>
        <tr><td class="gsc_rsb_sc1"><a>Citations</a></td><td class="gsc_rsb_std">{citations}</td><td class="gsc_rsb_std">0</td></tr>
        <tr><td class="gsc_rsb_sc1"><a>h-index</a></td><td class="gsc_rsb_std">{h_index}</td><td class="gsc_rsb_std">0</td></tr>
        <tr><td class="gsc_rsb_sc1"><a>i10-index</a></td><td class="gsc_rsb_std">0</td><td class="gsc_rsb_std">0</td></tr>
      </tbody></table>
      <table id="gsc_a_t"><tbody id="gsc_a_b">{rows_html}</tbody></table>
      {visible_range}<button id="gsc_bpf_more"{disabled}><span>Show more</span></button>
    </body></html>'''


class ParseTests(unittest.TestCase):
    def test_uses_all_time_metrics_and_preserves_zero_citations(self):
        page = parse_page(profile([
            article(1, title="A & B: PDEs", citations="1,234"),
            article(2, citations="", year=""),
            article(3, citations="0"),
        ], citations="1,234", h_index="1"), SCHOLAR_ID)
        self.assertEqual((page.total_citations, page.h_index), (1234, 1))
        self.assertEqual([paper["citations"] for paper in page.publications], [1234, 0, 0])
        self.assertEqual(page.publications[0]["title"], "A & B: PDEs")
        self.assertEqual(page.publications[0]["scholarId"], SCHOLAR_ID + ":paper_1")
        self.assertEqual(page.publications[0]["year"], 2026)
        self.assertNotIn("year", page.publications[1])
        self.assertFalse(page.has_more)

    def test_block_or_login_html_is_never_valid_zero_data(self):
        for html in ["<html>Sorry, unusual traffic; CAPTCHA</html>", "<form>Sign in</form>", ""]:
            with self.subTest(html=html), self.assertRaises(SyncError):
                parse_page(html, SCHOLAR_ID)

    def test_missing_metrics_pagination_citations_or_row_range_is_rejected(self):
        good = profile([article(1)])
        invalid = [
            good.replace('id="gsc_rsb_st"', 'id="unknown"'),
            good.replace('id="gsc_bpf_more"', 'id="unknown"'),
            good.replace('class="gsc_a_ac gs_ibl"', 'class="unknown"'),
            good.replace("1–1", "1–7"),
            good.replace('class="gsc_a_y"', 'class="unknown"'),
        ]
        for html in invalid:
            with self.subTest(html=html), self.assertRaises(SyncError):
                parse_page(html, SCHOLAR_ID)

    def test_wrong_profile_and_invalid_metric_are_rejected(self):
        for html in [
            profile([article(1)]).replace(SCHOLAR_ID + ":", "other_profile:"),
            profile([article(1)], citations="-1"),
            profile([article(1)], h_index="unavailable"),
        ]:
            with self.subTest(html=html), self.assertRaises(SyncError):
                parse_page(html, SCHOLAR_ID)

    def test_short_nonterminal_page_is_rejected(self):
        with self.assertRaisesRegex(SyncError, "incomplete"):
            parse_page(profile([article(1)], more=True), SCHOLAR_ID)

    def test_only_explicit_empty_profile_is_accepted(self):
        self.assertEqual(parse_page(profile([]), SCHOLAR_ID).publications, [])
        with self.assertRaises(SyncError):
            parse_page(profile([]).replace("No articles available.", "Loading…"), SCHOLAR_ID)
        with self.assertRaises(SyncError):
            parse_page(profile([], citations="3"), SCHOLAR_ID)


class PaginationTests(unittest.TestCase):
    def test_collects_all_pages_and_advances_start(self):
        urls = []

        def fetch(url):
            urls.append(url)
            start = int(parse_qs(urlparse(url).query)["cstart"][0])
            if start == 0:
                return profile([article(index) for index in range(100)], more=True)
            self.assertEqual(start, 100)
            return profile([article(100)], start=100)

        result = collect_profile(SCHOLAR_ID, fetch=fetch)
        self.assertEqual(len(result["publications"]), 101)
        self.assertEqual(len(urls), 2)
        self.assertTrue(result["updatedAt"].endswith("Z"))

    def test_repeated_ids_are_rejected(self):
        with self.assertRaisesRegex(SyncError, "Repeated publication"):
            collect_profile(SCHOLAR_ID, fetch=lambda _: profile([article(1), article(1)]))

    def test_changing_metrics_or_blocked_later_pages_are_rejected(self):
        first = profile([article(index) for index in range(100)], citations="1", more=True)
        for second in [profile([article(100)], citations="2", start=100), "<html>CAPTCHA</html>"]:
            pages = iter([first, second])
            with self.subTest(second=second), self.assertRaises(SyncError):
                collect_profile(SCHOLAR_ID, fetch=lambda _: next(pages))

    def test_pagination_cap_does_not_return_partial_result(self):
        with self.assertRaisesRegex(SyncError, "pagination limit"):
            collect_profile(SCHOLAR_ID, fetch=lambda _: profile([article(index) for index in range(100)], more=True), max_pages=1)

    def test_h_index_cannot_exceed_complete_paper_count(self):
        with self.assertRaisesRegex(SyncError, "inconsistent"):
            collect_profile(SCHOLAR_ID, fetch=lambda _: profile([article(1)], citations="100", h_index="2"))


class PendingTests(unittest.TestCase):
    def test_matches_stable_id_or_unicode_normalized_title(self):
        snapshot = collect_profile(SCHOLAR_ID, fetch=lambda _: profile([
            article(1, title="Renamed paper"),
            article(2, title="ＰＩＮＮ: A—B method"),
            article(3, title="New result"),
        ]))
        pending = pending_publications(snapshot, [
            {"title": "Old title", "scholarId": SCHOLAR_ID + ":paper_1"},
            {"title": "pinn a-b METHOD"},
        ])
        self.assertEqual([paper["title"] for paper in pending["publications"]], ["New result"])
        self.assertEqual(pending["updatedAt"], snapshot["updatedAt"])


class TransportTests(unittest.TestCase):
    def test_access_denied_is_not_retried(self):
        url = f"https://scholar.google.com/citations?user={SCHOLAR_ID}"
        with patch("sync_scholar.urlopen", side_effect=HTTPError(url, 403, "Forbidden", {}, None)) as request:
            with self.assertRaisesRegex(SyncError, "HTTP 403"):
                fetch_html(url, deadline=time.monotonic() + 120)
        self.assertEqual(request.call_count, 1)

    def test_transient_failure_has_bounded_retries(self):
        url = f"https://scholar.google.com/citations?user={SCHOLAR_ID}"
        with patch("sync_scholar.urlopen", side_effect=URLError("timeout")) as request, patch("sync_scholar.time.sleep"):
            with self.assertRaisesRegex(SyncError, "request failed"):
                fetch_html(url, deadline=time.monotonic() + 120, attempts=3)
        self.assertEqual(request.call_count, 3)

    def test_expired_deadline_makes_no_request(self):
        with patch("sync_scholar.urlopen") as request, self.assertRaisesRegex(SyncError, "time limit"):
            fetch_html("https://scholar.google.com/citations", deadline=time.monotonic() - 1)
        request.assert_not_called()


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "scholar.json"
        self.pending = self.root / "pending.json"
        self.curated = self.root / "publications.json"
        # Baseline intentionally has no Scholar publication IDs and no timestamp.
        self.old_output = b'{"updatedAt":null,"totalCitations":59,"publications":[{"title":"Old paper","citations":3}]}\n'
        self.old_pending = b'{"updatedAt":null,"publications":[]}\n'
        self.output.write_bytes(self.old_output)
        self.pending.write_bytes(self.old_pending)
        self.curated.write_text('[{"title":"Paper 1"}]', encoding="utf-8")

    def assertRetained(self):
        self.assertEqual(self.output.read_bytes(), self.old_output)
        self.assertEqual(self.pending.read_bytes(), self.old_pending)

    def test_blocked_fetch_retains_both_existing_files_byte_for_byte(self):
        with self.assertRaises(SyncError):
            sync(SCHOLAR_ID, self.output, self.curated, self.pending, fetch=lambda _: "<html>CAPTCHA</html>")
        self.assertRetained()

    def test_invalid_curated_file_retains_both_outputs(self):
        self.curated.write_text('[{"title":null}]', encoding="utf-8")
        with self.assertRaises(SyncError):
            sync(SCHOLAR_ID, self.output, self.curated, self.pending, fetch=lambda _: profile([article(1)]))
        self.assertRetained()

    def test_success_replaces_both_outputs_and_excludes_curated_paper(self):
        result = sync(SCHOLAR_ID, self.output, self.curated, self.pending, fetch=lambda _: profile([article(1), article(2)]))
        self.assertEqual(json.loads(self.output.read_text()), result)
        self.assertEqual([entry["title"] for entry in json.loads(self.pending.read_text())["publications"]], ["Paper 2"])
        self.assertEqual(len(list(self.root.glob(".*.tmp"))), 0)

    def test_second_output_replace_failure_rolls_back_first_output(self):
        import os
        replace = os.replace
        calls = 0

        def fail_second(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("Simulated second-file replacement failure")
            return replace(source, destination)

        with patch("sync_scholar.os.replace", side_effect=fail_second), self.assertRaises(OSError):
            write_outputs({self.output: {"new": 1}, self.pending: {"new": 2}})
        self.assertRetained()
        self.assertEqual(len(list(self.root.glob(".*.tmp"))), 0)


if __name__ == "__main__":
    unittest.main()
