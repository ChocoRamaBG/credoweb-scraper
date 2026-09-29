"""Recurring runs must refresh data, retain good records and make bounded progress."""
import io
import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from credoweb_discovery import discover
from credoweb_profiles import fetch_profile
from credoweb_scraper import (
    APIError, BudgetExpired, Client, arguments, begin_discovery_refresh,
    enrichment_listings, finish_discovery_refresh, main,
)


class WeeklyRefreshTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)
        self.client = Client(self.output / "cache.sqlite3", delay=0, retries=0, refresh_days=6)
        self.addCleanup(self.client.close)

    def cache(self, route, data, when):
        self.client.db.execute("INSERT OR REPLACE INTO responses VALUES (?,?,?)",
                               (self.client.url(route), json.dumps(data), when))
        self.client.db.commit()

    def test_expired_responses_are_refetched_but_failed_requests_keep_saved_response(self):
        self.cache("search", {"old": True}, "2000-01-01T00:00:00+00:00")
        with patch.object(self.client.opener, "open", return_value=io.BytesIO(b'{"data":{"new":true}}')) as opener:
            self.assertEqual(self.client.get("search"), {"new": True})
            self.assertEqual(self.client.get("search"), {"new": True})
        self.assertEqual(opener.call_count, 1)
        self.cache("profile/1/businessCard", {"phone": "0123"}, "2000-01-01T00:00:00+00:00")
        with patch.object(self.client.opener, "open", side_effect=HTTPError("url", 503, "down", {}, None)):
            with self.assertRaises(APIError):
                self.client.get("profile/1/businessCard")
        data = self.client.db.execute("SELECT data FROM responses WHERE url=?", (self.client.url("profile/1/businessCard"),)).fetchone()[0]
        self.assertEqual(json.loads(data), {"phone": "0123"})

    def test_unfinished_discovery_keeps_same_cutoff_across_weekly_runs(self):
        begin_discovery_refresh(self.client)
        original = self.client.search_cutoff
        self.client.refresh_cutoff += timedelta(days=7)
        begin_discovery_refresh(self.client)
        self.assertEqual(self.client.search_cutoff, original)
        finish_discovery_refresh(self.client)
        self.client.refresh_cutoff = datetime.now(timezone.utc) + timedelta(days=1)
        begin_discovery_refresh(self.client)
        self.assertEqual(self.client.search_cutoff, self.client.refresh_cutoff)

    def test_profile_refresh_generation_reuses_finished_pages_after_another_week(self):
        self.cache("profile/1?module=tabPublicationPublished", {"description": "Saved page"}, "2026-01-02T00:00:00+00:00")
        self.assertIsNone(self.client.get_cached("profile/1?module=tabPublicationPublished"))
        self.client.profile_scope = "/api/profile/1"
        self.client.profile_cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch.object(self.client.opener, "open", side_effect=AssertionError("Must resume cached pages")):
            self.assertEqual(self.client.get("profile/1?module=tabPublicationPublished"), {"description": "Saved page"})

    def test_identity_sections_expire_even_during_a_long_running_profile_refresh(self):
        self.cache("profile/1?module=tabContacts", {"email": "old@example.bg"}, "2026-01-02T00:00:00+00:00")
        self.client.profile_scope = "/api/profile/1"
        self.client.profile_cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertIsNone(self.client.get_cached("profile/1?module=tabContacts"))

    def test_only_registered_recovery_pages_survive_a_new_primary_sweep(self):
        route = "search?cat=101&location=123&page=0"
        self.cache(route, {"result": []}, "2000-01-01T00:00:00+00:00")
        self.assertIsNone(self.client.get_cached(route))
        self.assertIsNone(self.client.get_recovery_cached(route))
        self.client.remember_recovery(route)
        self.assertEqual(self.client.get_recovery_cached(route), {"result": []})
        self.assertIsNone(self.client.get_cached(route))

    def test_discovery_distinguishes_coverage_gap_from_failed_primary_request(self):
        first = {"result": [{"profileId": 1}], "totalCount": 2, "pageCount": 2}
        for second, coherent in ((first, True), (APIError("temporary failure", 503), False)):
            with self.subTest(coherent=coherent), patch.object(self.client, "get", side_effect=[first, second]):
                report = discover(self.client, [101], max_recovery_requests=0)
            self.assertFalse(report["complete"])
            self.assertEqual(report["traversal_complete"], coherent)
            self.assertEqual(report["primary_request_errors"], 0 if coherent else 1)

    def test_discovery_updates_existing_listing_from_fresh_page(self):
        previous = {"profileId": 1, "basicInfo": {"title": "Old name"}}
        current = {"profileId": 1, "basicInfo": {"title": "Updated name"}}
        with patch.object(self.client, "get", return_value={"result": [current], "totalCount": 1, "pageCount": 1}):
            discover(self.client, [101], initial_items=[(101, previous)], on_item=self.client.add_listing)
        saved = json.loads(self.client.db.execute("SELECT data FROM listings WHERE profile_id=1").fetchone()[0])
        self.assertEqual(saved, current)

    def test_fair_queue_interleaves_new_and_due_profiles_and_rotates_failed_attempts(self):
        for profile_id in range(1, 6):
            self.client.add_listing(101, {"profileId": profile_id})
        for profile_id, status, fetched in ((1, "partial", "2000-01-01"), (3, "complete", "2000-01-01"),
                                            (4, "complete", datetime.now(timezone.utc).isoformat())):
            self.client.save_profile({"profile_id": profile_id, "category": 101, "status": status,
                                      "fetched_at": fetched, "sections": {}})
        self.client.db.execute("INSERT INTO profile_attempts VALUES (?,?)", (1, datetime.now(timezone.utc).isoformat()))
        self.client.db.commit()
        self.assertEqual([item["profileId"] for _, item in enrichment_listings(self.client, [101], None)], [2, 3, 5, 1])

    def test_budget_does_not_wait_for_a_long_retry_after(self):
        self.client.retries = 2
        with patch("credoweb_scraper.time.monotonic", return_value=100), patch("credoweb_scraper.time.sleep") as sleep:
            self.client.deadlines["run"] = 110
            with patch.object(self.client.opener, "open", side_effect=HTTPError("url", 429, "wait", {"Retry-After": "3600"}, None)):
                with self.assertRaises(BudgetExpired) as raised:
                    self.client.get("search")
        self.assertEqual(raised.exception.kind, "run")
        self.assertNotIn(unittest.mock.call(3600), sleep.call_args_list)

    def test_failed_refresh_preserves_previous_contacts_and_address(self):
        previous = {"status": "complete", "fetched_at": "2000-01-01", "sections": {
            "businessCard": {"address": "Known full address"}, "tabContacts": {"email": "known@example.bg"}}}
        def get(route, params=None):
            if (params or {}).get("module") == "subNavigation":
                return {"navigation": [{"backendRoute": "profile/1?module=tabContacts"}]}
            raise APIError("temporarily unavailable", 503)
        checkpoints = []
        with patch.object(self.client, "get", side_effect=get):
            record = fetch_profile(self.client, {"profileId": 1}, 101, previous_record=previous,
                                   on_section=lambda value: checkpoints.append(json.loads(json.dumps(value))))
        self.assertEqual(record["status"], "partial")
        self.assertEqual(record["sections"]["businessCard"]["address"], "Known full address")
        self.assertEqual(record["sections"]["tabContacts"]["email"], "known@example.bg")
        self.assertEqual(record["retained_sections"], ["businessCard", "tabContacts"])
        self.assertTrue(all("tabContacts" in saved["sections"] for saved in checkpoints))
        self.assertEqual(previous["sections"]["tabContacts"], {"email": "known@example.bg"})

    def test_successful_refresh_removes_sections_no_longer_advertised(self):
        previous = {"status": "complete", "sections": {"oldSection": {"obsolete": True}, "businessCard": {"old": True}}}
        with patch.object(self.client, "get", side_effect=[{"new": True}, {"navigation": []}]):
            result = fetch_profile(self.client, {"profileId": 1}, 101, previous_record=previous)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(set(result["sections"]), {"businessCard", "subNavigation"})
        self.assertEqual(result["retained_sections"], [])

    def test_budget_options_require_finite_positive_values(self):
        for option in ("--refresh-days", "--max-runtime-seconds", "--discovery-budget-seconds", "--profile-budget-seconds"):
            for value in ("0", "-1", "nan", "inf"):
                with self.subTest(option=option, value=value), patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
                    arguments([option, value])
        self.assertEqual(arguments(["--max-runtime-seconds", "300"]).profile_budget_seconds, 120)


class WeeklyRunTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)
        self.clock = [1000.0]
        for context in (
            patch("credoweb_scraper.logging.FileHandler", return_value=logging.NullHandler()),
            patch("credoweb_scraper.time.monotonic", side_effect=lambda: self.clock[0]),
            patch("credoweb_export.export_records", side_effect=lambda records, *args, **kwargs: {"rows": len(list(records))}),
        ):
            context.start()
            self.addCleanup(context.stop)

    def seed(self, complete=False):
        client = Client(self.output / "checkpoint.sqlite3")
        for profile_id in (1, 2):
            client.add_listing(101, {"profileId": profile_id, "basicInfo": {"title": f"Doctor {profile_id}"}})
            if complete:
                record = self.record(profile_id)
                record["fetched_at"] = "2000-01-01" if profile_id == 1 else datetime.now(timezone.utc).isoformat()
                client.save_profile(record)
        client.close()

    @staticmethod
    def record(profile_id, status="complete"):
        return {"profile_id": profile_id, "category": 101, "status": status, "fetched_at": datetime.now(timezone.utc).isoformat(),
                "sections": {"businessCard": {"title": f"Doctor {profile_id}"}}, "errors": []}

    def manifest(self):
        return json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))

    def test_overall_budget_exits_cleanly_and_exports_all_known_rows(self):
        self.seed()
        def fetch(client, item, category, **kwargs):
            kwargs["on_section"](self.record(item["profileId"], "in_progress"))
            self.clock[0] += 101
            client.check_budget()
        with patch("credoweb_profiles.fetch_profile", side_effect=fetch):
            result = main(["--output", str(self.output), "--categories", "101", "--enrich-only", "--max-runtime-seconds", "100", "--quick-export"])
        self.assertEqual(result, 0)
        self.assertEqual(self.manifest()["status"], "budget_exhausted")
        self.assertEqual(self.manifest()["counts"]["exported_profiles"], 2)
        self.assertEqual(self.manifest()["merge_export_status"], "complete")
        self.assertTrue((self.output / "merge/profiles.csv").exists())

    def test_discovery_budget_moves_on_to_enrichment_and_retains_listing(self):
        self.seed()
        def discover(client, categories, **kwargs):
            self.assertEqual(list(kwargs["initial_items"]), [])
            client.add_listing(101, {"profileId": 3})
            kwargs["on_checkpoint"]({"complete": False, "categories": []})
            self.clock[0] += 31
            client.check_budget()
        with patch("credoweb_discovery.discover", side_effect=discover), patch("credoweb_profiles.fetch_profile", side_effect=lambda client, item, cat, **kw: self.record(item["profileId"])) as fetch:
            result = main(["--output", str(self.output), "--categories", "101", "--refresh-days", "6", "--max-runtime-seconds", "100", "--discovery-budget-seconds", "30", "--quick-export"])
        self.assertEqual(result, 2)
        self.assertEqual(fetch.call_count, 3)
        self.assertTrue(self.manifest()["discovery"]["budget_exhausted"])
        self.assertEqual(self.manifest()["counts"]["exported_profiles"], 3)

    def test_one_long_profile_cannot_starve_following_profiles(self):
        self.seed()
        def fetch(client, item, category, **kwargs):
            if item["profileId"] == 1:
                kwargs["on_section"](self.record(1, "in_progress"))
                self.clock[0] += 11
                client.check_budget()
            return self.record(item["profileId"])
        with patch("credoweb_profiles.fetch_profile", side_effect=fetch) as mocked:
            result = main(["--output", str(self.output), "--categories", "101", "--enrich-only", "--max-runtime-seconds", "100", "--profile-budget-seconds", "10", "--quick-export"])
        self.assertEqual(result, 2)
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(self.manifest()["profiles_deferred_by_budget"], 1)
        self.assertEqual(self.manifest()["counts"]["profile_statuses"], {"in_progress": 1, "complete": 1})

    def test_stale_complete_profile_is_refreshed_and_recent_complete_is_skipped(self):
        self.seed(complete=True)
        with patch("credoweb_profiles.fetch_profile", side_effect=lambda client, item, cat, **kw: self.record(item["profileId"])) as fetch:
            main(["--output", str(self.output), "--categories", "101", "--enrich-only", "--refresh-days", "6", "--quick-export"])
        self.assertEqual([call.args[1]["profileId"] for call in fetch.call_args_list], [1])
        self.assertEqual(fetch.call_args.kwargs["previous_record"]["status"], "complete")

    def test_only_a_coherent_primary_traversal_finishes_refresh_generation(self):
        self.seed()
        for coherent in (False, True):
            report = {"complete": False, "traversal_complete": coherent, "categories": []}
            with self.subTest(coherent=coherent), patch("credoweb_discovery.discover", return_value=report), patch("credoweb_scraper.finish_discovery_refresh") as finish:
                main(["--output", str(self.output), "--categories", "101", "--directory-only", "--refresh-days", "6", "--quick-export"])
            self.assertEqual(finish.call_count, 1 if coherent else 0)


if __name__ == "__main__":
    unittest.main()
