"""Offline integration checks for resumable collection and local exports."""
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from credoweb_scraper import APIError, Client, main, prioritize_facility_cards, selected_listings


class CLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)
        # Avoid persistent logging handles on the temporary Windows directory.
        self.logging_patch = patch("credoweb_scraper.logging.FileHandler", return_value=logging.NullHandler())
        self.logging_patch.start()
        self.addCleanup(self.logging_patch.stop)
        self.exported = []
        self.export_history = []

    @staticmethod
    def record(profile_id, status="complete"):
        return {"profile_id": profile_id, "category": 101, "status": status,
                "sections": {"businessCard": {"title": f"Doctor {profile_id}"}}, "errors": []}

    def discover(self, client, categories, **kwargs):
        # The real discovery consumes this database-backed resume iterator.
        list(kwargs.get("initial_items", []))
        for profile_id in (1, 2):
            kwargs["on_item"](101, {"profileId": profile_id, "basicInfo": {"title": f"Doctor {profile_id}"}})
        return {"complete": True}

    def export(self, records, output, **kwargs):
        self.exported = list(records)
        self.export_history.append(self.exported)
        return {"test": "offline"}

    def run_main(self, fetch, extra=None):
        with patch("credoweb_discovery.discover", side_effect=self.discover), \
             patch("credoweb_profiles.fetch_profile", side_effect=fetch) as mocked_fetch, \
             patch("credoweb_export.export_records", side_effect=self.export):
            result = main(["--output", str(self.output), "--categories", "101", *(extra or [])])
        return result, mocked_fetch

    def manifest(self):
        return json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))

    def test_completed_profiles_are_skipped_but_partial_profiles_retry(self):
        client = Client(self.output / "checkpoint.sqlite3")
        for profile_id, status in ((1, "complete"), (2, "partial")):
            record = self.record(profile_id, status)
            client.db.execute("INSERT INTO profiles VALUES (?,?,?,?)",
                              (profile_id, 101, status, json.dumps(record)))
        client.db.commit()
        client.close()
        result, fetch = self.run_main(lambda client, item, category, **kwargs: self.record(item["profileId"]))
        self.assertEqual(result, 0)
        self.assertEqual([call.args[1]["profileId"] for call in fetch.call_args_list], [2])
        self.assertEqual(self.manifest()["status"], "complete")
        self.assertEqual(len(self.exported), 2)

    def test_interrupt_exports_saved_profiles_and_resume_finishes_remaining(self):
        def interrupted(client, item, category, **kwargs):
            # Search directory rows are published before the first detail call.
            self.assertEqual([record["profile_id"] for record in self.export_history[1]], [1, 2])
            if item["profileId"] == 2:
                raise KeyboardInterrupt
            return self.record(item["profileId"])
        result, _ = self.run_main(interrupted)
        self.assertEqual(result, 130)
        self.assertEqual(self.manifest()["status"], "interrupted")
        self.assertEqual(self.manifest()["counts"]["discovered_unique_profiles"], 2)
        self.assertEqual([(record["profile_id"], record["status"]) for record in self.exported],
                         [(1, "complete"), (2, "listed")])
        self.assertTrue((self.output / "raw/profiles/1.json").exists())
        result, fetch = self.run_main(lambda client, item, category, **kwargs: self.record(item["profileId"]))
        self.assertEqual(result, 0)
        self.assertEqual([call.args[1]["profileId"] for call in fetch.call_args_list], [2])
        self.assertEqual(self.manifest()["status"], "complete")
        self.assertEqual(self.manifest()["counts"]["exported_profiles"], 2)

    def test_export_only_does_not_discover_or_fetch(self):
        self.run_main(lambda client, item, category, **kwargs: self.record(item["profileId"]))
        with patch("credoweb_discovery.discover") as discover, \
             patch("credoweb_profiles.fetch_profile") as fetch, \
             patch("credoweb_export.export_records", side_effect=self.export):
            result = main(["--output", str(self.output), "--export-only"])
        self.assertEqual(result, 0)
        discover.assert_not_called()
        fetch.assert_not_called()
        self.assertEqual(len(self.exported), 2)
        self.assertEqual(self.manifest()["status"], "complete")

    def test_mismatched_categories_do_not_overwrite_previous_manifest(self):
        self.run_main(lambda client, item, category, **kwargs: self.record(item["profileId"]))
        before = (self.output / "manifest.json").read_bytes()
        result = main(["--output", str(self.output), "--categories", "103"])
        self.assertEqual(result, 1)
        self.assertEqual((self.output / "manifest.json").read_bytes(), before)

    def test_failed_export_is_reported_and_export_only_can_recover(self):
        def locked_final_export(records, output, **kwargs):
            if kwargs.get("include_details"):
                raise PermissionError("File is open in Excel")
            return self.export(records, output, **kwargs)
        with patch("credoweb_discovery.discover", side_effect=self.discover), \
             patch("credoweb_profiles.fetch_profile", side_effect=lambda client, item, category, **kwargs: self.record(item["profileId"])), \
             patch("credoweb_export.export_records", side_effect=locked_final_export):
            result = main(["--output", str(self.output), "--categories", "101"])
        self.assertEqual(result, 1)
        self.assertEqual(self.manifest()["status"], "failed")
        self.assertEqual(self.manifest()["export_status"], "failed")
        self.assertEqual(self.manifest()["crawl_status"], "complete")
        with patch("credoweb_export.export_records", side_effect=self.export):
            result = main(["--output", str(self.output), "--export-only"])
        self.assertEqual(result, 0)
        self.assertEqual(self.manifest()["status"], "complete")
        self.assertEqual(self.manifest()["export_status"], "complete")
        self.assertNotIn("export_error", self.manifest())

    def test_directory_only_exports_all_listings_without_fetching_details(self):
        result, fetch = self.run_main(lambda *args, **kwargs: self.fail("Detail fetch in directory mode"),
                                      ["--directory-only"])
        self.assertEqual(result, 0)
        fetch.assert_not_called()
        self.assertEqual(self.manifest()["status"], "directory_complete")
        self.assertEqual(self.manifest()["counts"]["exported_profiles"], 2)
        self.assertEqual(self.manifest()["counts"]["detailed_profiles"], 0)
        self.assertEqual(self.manifest()["counts"]["pending_details"], 2)
        self.assertEqual([record["status"] for record in self.exported], ["listed", "listed"])

    def test_enrich_only_skips_discovery_and_retries_in_progress(self):
        self.run_main(lambda *args, **kwargs: self.fail("Unexpected fetch"), ["--directory-only"])
        client = Client(self.output / "checkpoint.sqlite3")
        client.save_profile(self.record(1))
        client.save_profile(self.record(2, "in_progress"))
        client.close()
        with patch("credoweb_discovery.discover") as discover, \
             patch("credoweb_profiles.fetch_profile", side_effect=lambda client, item, category, **kwargs: self.record(item["profileId"])) as fetch, \
             patch("credoweb_export.export_records", side_effect=self.export):
            result = main(["--output", str(self.output), "--categories", "101", "--enrich-only"])
        self.assertEqual(result, 0)
        discover.assert_not_called()
        self.assertEqual([call.args[1]["profileId"] for call in fetch.call_args_list], [2])
        self.assertEqual(self.manifest()["status"], "complete")

    def test_interrupt_inside_profile_preserves_contacts_in_database_and_export(self):
        def single_doctor(client, categories, **kwargs):
            list(kwargs.get("initial_items", []))
            kwargs["on_item"](101, {"profileId": 123, "basicInfo": {"title": "Doctor", "slug": "doctor"}})
            return {"complete": True}
        def get(client, route, params=None):
            module = (params or {}).get("module")
            if route.endswith("/businessCard"):
                return {"title": "Doctor"}
            if module == "subNavigation":
                return {"navigation": [{"backendRoute": f"profile/123?module={name}"}
                                       for name in ("tabPublicationPublished", "tabContacts", "about")]}
            if module == "about":
                return {"about": {"description": "Biography"}}
            if module == "tabContacts":
                return {"phones": ["0123456789"]}
            if module == "tabPublicationPublished" and (params or {}).get("page") is None:
                return {"contentList": [{"id": 1}], "page": 1, "pageCount": 500, "isLastPage": False}
            raise KeyboardInterrupt()
        with patch("credoweb_discovery.discover", side_effect=single_doctor), \
             patch.object(Client, "get", new=get), \
             patch("credoweb_export.export_records", side_effect=self.export):
            result = main(["--output", str(self.output), "--categories", "101"])
        self.assertEqual(result, 130)
        self.assertEqual(self.manifest()["counts"]["exported_profiles"], 1)
        self.assertEqual(self.manifest()["counts"]["profile_statuses"], {"in_progress": 1})
        self.assertEqual(self.exported[0]["sections"]["tabContacts"]["phones"], ["0123456789"])
        connection = sqlite3.connect(self.output / "checkpoint.sqlite3")
        try:
            saved = json.loads(connection.execute("SELECT data FROM profiles WHERE profile_id=123").fetchone()[0])
        finally:
            connection.close()
        self.assertEqual(saved["status"], "in_progress")
        self.assertEqual(saved["sections"]["tabContacts"]["phones"], ["0123456789"])
        self.assertEqual(json.loads((self.output / "raw/profiles/123.json").read_text(encoding="utf-8")), saved)

    def test_section_checkpoints_publish_periodic_reports_before_profile_finishes(self):
        clock = [1000.0]
        def enriching(client, item, category, **kwargs):
            record = self.record(item["profileId"], "in_progress")
            record["sections"]["tabContacts"] = {"phones": ["0123456789"]}
            clock[0] += 130
            kwargs["on_section"](record)
            # Assert while the profile is still running, before the final export.
            self.assertEqual(len(self.export_history), 3)
            current = {entry["profile_id"]: entry for entry in self.export_history[-1]}
            self.assertEqual(current[1]["status"], "in_progress")
            self.assertEqual(current[1]["sections"]["tabContacts"]["phones"], ["0123456789"])
            self.assertEqual(current[2]["status"], "listed")
            raise KeyboardInterrupt()
        with patch("credoweb_scraper.time.monotonic", side_effect=lambda: clock[0]):
            result, _ = self.run_main(enriching)
        self.assertEqual(result, 130)
        self.assertEqual(self.manifest()["counts"]["exported_profiles"], 2)

    def test_facility_cards_are_saved_before_deep_profiles_and_respect_sample_selection(self):
        calls = []
        def mixed_directory(client, categories, **kwargs):
            list(kwargs.get("initial_items", []))
            for category, profile_id in ((101, 1), (101, 2), (103, 3), (103, 4)):
                kwargs["on_item"](category, {"profileId": profile_id, "profileType": "page" if category == 103 else "user",
                                           "basicInfo": {"title": f"Name {profile_id}"}})
            return {"complete": True}
        def get(client, route, params=None):
            calls.append(("card", route))
            return {"address": "България, София, 1000, ул. Тест 1"}
        def deep(client, item, category, **kwargs):
            calls.append(("deep", item["profileId"]))
            saved = json.loads(client.db.execute("SELECT data FROM profiles WHERE profile_id=3").fetchone()[0])
            self.assertIn("businessCard", saved["sections"])
            record = self.record(item["profileId"])
            record["category"] = category
            return record
        with patch("credoweb_discovery.discover", side_effect=mixed_directory), \
             patch.object(Client, "get", new=get), \
             patch("credoweb_profiles.fetch_profile", side_effect=deep), \
             patch("credoweb_export.export_records", side_effect=self.export):
            result = main(["--output", str(self.output), "--max-profiles", "3"])
        self.assertEqual(result, 0)
        self.assertEqual(calls, [("card", "profile/3/businessCard"), ("deep", 1), ("deep", 3), ("deep", 2)])
        self.assertEqual(self.manifest()["counts"]["exported_profiles"], 4)

    def test_facility_priority_reuses_cache_and_preserves_existing_sections_and_status(self):
        client = Client(self.output / "checkpoint.sqlite3")
        self.addCleanup(client.close)
        for profile_id in (10, 11, 12, 13):
            client.add_listing(103, {"profileId": profile_id, "basicInfo": {"title": f"Facility {profile_id}"}})
        for profile_id, status, sections in (
            (10, "complete", {}), (11, "partial", {"about": {"description": "Keep this"}}),
            (12, "in_progress", {"businessCard": {"address": "Existing address"}}),
        ):
            client.save_profile({"profile_id": profile_id, "category": 103, "status": status,
                                 "sections": sections, "errors": [{"message": "Keep existing error"}]})
        for profile_id in (11, 13):
            client.db.execute("INSERT INTO responses VALUES (?,?,?)", (
                client.url(f"profile/{profile_id}/businessCard"), json.dumps({"address": f"Address {profile_id}"}), "2026-09-29"))
        client.db.commit()
        with patch.object(client.opener, "open", side_effect=AssertionError("Cache must avoid HTTP")):
            prioritize_facility_cards(client, selected_listings(client, [103], None), client.save_profile)
        saved = {record["profile_id"]: record for record in client.records()}
        self.assertEqual(client.requests, 0)
        self.assertEqual(client.cache_hits, 2)
        self.assertEqual(saved[11]["status"], "partial")
        self.assertEqual(saved[11]["sections"]["about"]["description"], "Keep this")
        self.assertEqual(saved[11]["errors"], [{"message": "Keep existing error"}])
        self.assertEqual(saved[12]["sections"]["businessCard"]["address"], "Existing address")
        self.assertEqual(saved[13]["status"], "in_progress")
        self.assertEqual(saved[13]["sections"]["businessCard"]["address"], "Address 13")

    def test_failed_priority_card_does_not_stop_other_facilities_and_full_fetch_retries(self):
        from credoweb_profiles import fetch_profile
        client = Client(self.output / "checkpoint.sqlite3")
        self.addCleanup(client.close)
        for profile_id in (20, 21):
            client.add_listing(103, {"profileId": profile_id, "basicInfo": {"title": f"Facility {profile_id}"}})
        attempts = []
        def get(route, params=None):
            attempts.append(route)
            if route == "profile/20/businessCard" and attempts.count(route) == 1:
                raise APIError("HTTP 503", status=503)
            if (params or {}).get("module") == "subNavigation":
                return {"navigation": []}
            return {"address": "Full public address"}
        with patch.object(client, "get", side_effect=get):
            prioritize_facility_cards(client, selected_listings(client, [103], None), client.save_profile)
            saved = {record["profile_id"]: record for record in client.records()}
            self.assertEqual(saved[20]["errors"][0]["status"], 503)
            self.assertIn("businessCard", saved[21]["sections"])
            retried = fetch_profile(client, saved[20]["listing"], 103)
        self.assertEqual(attempts.count("profile/20/businessCard"), 2)
        self.assertEqual(retried["status"], "complete")
        self.assertEqual(retried["errors"], [])


if __name__ == "__main__":
    unittest.main()
