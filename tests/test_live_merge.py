"""Offline checks for publishing clean CSVs during the normal collection run."""

import copy
import csv
import hashlib
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import credoweb_merge
from credoweb_scraper import main


def live_record(profile_id=123, name="Д-р Пример", status="complete"):
    return {
        "profile_id": profile_id, "category": 101, "status": status, "errors": [],
        "url": f"https://www.credoweb.bg/profile/{profile_id}/example",
        "sections": {
            "businessCard": {"title": name, "city": {"label": "София"}},
            "about": {"practiceList": [{
                "institution": {"id": 87, "label": "МЦ Пример", "city": {"label": "Варна"}},
                "location": {"address": "ул. Пример 7", "postCode": {"label": "0123"},
                             "country": {"label": "България"}},
                "contactList": {"phoneNumbers": ["0888123456"], "email": "office@example.bg"},
            }]},
        },
    }


def read_csv(folder, filename="profiles.csv"):
    with (Path(folder) / filename).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


class LiveMergeWriterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "merge"

    def test_new_export_contains_four_csvs_schema_readme_and_matching_manifest_hashes(self):
        snapshot_at = "2026-09-29T12:34:56+00:00"
        result = credoweb_merge.export_merge_records(
            [live_record()], self.output, snapshot_at=snapshot_at, collection_status="running")
        expected = {"profiles.csv", "workplaces.csv", "contacts.csv", "specialties.csv",
                    "schema.json", "README.md", "manifest.json"}
        self.assertEqual({path.name for path in self.output.iterdir()}, expected)
        self.assertEqual(result["snapshot_at"], snapshot_at)
        self.assertEqual(result["counts"]["profiles"], 1)
        manifest = json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["snapshot_at"], snapshot_at)
        self.assertEqual(manifest["collection_status"], "running")
        self.assertEqual(manifest["publication_status"], "complete")
        self.assertEqual(manifest["counts"], result["counts"])
        self.assertEqual(set(manifest["files"]), set(credoweb_merge.TABLE_COLUMNS))
        for table, columns in credoweb_merge.TABLE_COLUMNS.items():
            filename = table + ".csv"
            data = (self.output / filename).read_bytes()
            metadata = manifest["files"][table]
            self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(metadata["filename"], filename)
            self.assertEqual(metadata["bytes"], len(data))
            self.assertEqual(metadata["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(metadata["rows"], len(read_csv(self.output, filename)))
            with (self.output / filename).open(encoding="utf-8-sig", newline="") as handle:
                self.assertEqual(csv.DictReader(handle).fieldnames, columns)
        for metadata in manifest["support_files"].values():
            data = (self.output / metadata["filename"]).read_bytes()
            self.assertEqual(metadata["sha256"], hashlib.sha256(data).hexdigest())
        self.assertIsInstance(json.loads((self.output / "schema.json").read_text(encoding="utf-8")), dict)
        self.assertIn("profile_id", (self.output / "README.md").read_text(encoding="utf-8"))

    def test_republish_replaces_rows_in_place_and_keeps_stable_entity_keys(self):
        first = live_record(status="in_progress")
        credoweb_merge.export_merge_records([first], self.output, collection_status="running")
        before = read_csv(self.output)[0]
        first["sections"]["businessCard"]["title"] = "Обновено име"
        first["sections"]["tabContacts"] = {"phone": "+359888000001"}
        first["status"] = "complete"
        credoweb_merge.export_merge_records(iter([first]), self.output, collection_status="complete")
        rows = read_csv(self.output)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["profile_id"], before["profile_id"])
        self.assertEqual(rows[0]["entity_key"], before["entity_key"])
        self.assertEqual(rows[0]["name"], "Обновено име")
        self.assertEqual(rows[0]["phone"], "+359888000001")
        self.assertEqual(rows[0]["record_status"], "complete")
        self.assertEqual(len(read_csv(self.output, "workplaces.csv")), 1)
        contacts = read_csv(self.output, "contacts.csv")
        self.assertEqual(len({row["contact_id"] for row in contacts}), len(contacts))

    def test_serialization_failure_preserves_previous_files_and_cleans_temporary_files(self):
        class UnserializableCell:
            def __str__(self):
                raise ValueError("Cannot serialize this CSV cell")

        record = live_record()
        credoweb_merge.export_merge_records([record], self.output)
        before = {path.name: path.read_bytes() for path in self.output.iterdir()}
        changed = copy.deepcopy(record)
        changed["sections"]["businessCard"]["title"] = "Should not publish"
        tables = credoweb_merge.build_tables([changed])
        # Profiles serialize successfully before the second table fails.
        tables["workplaces"][0]["facility_name"] = UnserializableCell()
        with patch("credoweb_merge.build_tables", return_value=tables):
            with self.assertRaises(ValueError):
                credoweb_merge.export_merge_records([changed], self.output)
        after = {path.name: path.read_bytes() for path in self.output.iterdir()}
        self.assertEqual(after, before)

    def test_city_only_workplace_does_not_hide_the_next_real_street_address(self):
        record = live_record()
        record["sections"]["about"]["practiceList"].insert(0, {
            "institution": {"id": 88, "label": "МЦ Без адрес", "city": {"label": "Пловдив"}}})
        tables = credoweb_merge.build_tables([record])
        profile = tables["profiles"][0]
        self.assertEqual(profile["workplace_count"], 2)
        self.assertEqual(profile["address_count"], 1)
        self.assertEqual(profile["address_city"], "Варна")
        self.assertEqual(profile["full_address"], "България, Варна, 0123, ул. Пример 7")
        sparse = next(row for row in tables["workplaces"] if row["facility_profile_id"] == "88")
        self.assertEqual(sparse["city"], "Пловдив")
        self.assertEqual(sparse["full_address"], "")

    def test_overlong_phone_value_is_retained_without_a_guessed_normalized_number(self):
        record = live_record()
        record["sections"]["tabContacts"] = {"phone": "0123456789012345"}
        contact = next(row for row in credoweb_merge.build_tables([record])["contacts"]
                       if row["contact_value"] == "0123456789012345")
        self.assertEqual(contact["contact_type"], "phone")
        self.assertEqual(contact["contact_normalized"], "")


class LiveMergeCLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)
        self.merge = self.output / "merge"
        self.log_patch = patch("credoweb_scraper.logging.FileHandler", return_value=logging.NullHandler())
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)
        # The report writer is mocked in this group; real paired exports are
        # covered by test_full_exports.
        seal = patch("credoweb_full.seal_full_exports")
        seal.start()
        self.addCleanup(seal.stop)

    @staticmethod
    def discover(client, categories, **kwargs):
        list(kwargs.get("initial_items", []))
        for profile_id in (123, 456):
            kwargs["on_item"](101, {"profileId": profile_id,
                                    "basicInfo": {"title": f"Doctor {profile_id}"}})
        return {"complete": True}

    def run_main(self, *, discover=None, fetch=None, report=None, extra=()):
        with patch("credoweb_discovery.discover", side_effect=discover or self.discover), \
             patch("credoweb_profiles.fetch_profile", side_effect=fetch or (
                 lambda client, item, category, **kwargs: live_record(item["profileId"]))), \
             patch("credoweb_export.export_records", side_effect=report or (lambda *args, **kwargs: {})):
            return main(["--output", str(self.output), "--categories", "101", *extra])

    def test_default_run_creates_startup_periodic_and_final_clean_csv_snapshots(self):
        clock = [1000.0]
        seen = []

        def discover(client, categories, **kwargs):
            self.assertTrue((self.merge / "profiles.csv").exists())
            self.assertEqual(read_csv(self.merge), [])
            seen.append("startup")
            return self.discover(client, categories, **kwargs)

        def fetch(client, item, category, **kwargs):
            if item["profileId"] == 123:
                listed = read_csv(self.merge)
                self.assertEqual({row["profile_id"] for row in listed}, {"123", "456"})
                self.assertEqual({row["record_status"] for row in listed}, {"listed"})
                seen.append("directory")
                partial = live_record(123, status="in_progress")
                partial["sections"]["tabContacts"] = {"phone": "+359888000001"}
                clock[0] += 130
                kwargs["on_section"](partial)
                rows = {row["profile_id"]: row for row in read_csv(self.merge)}
                self.assertEqual(rows["123"]["record_status"], "in_progress")
                self.assertEqual(rows["123"]["phone"], "+359888000001")
                self.assertEqual(rows["456"]["record_status"], "listed")
                seen.append("periodic")
            return live_record(item["profileId"], name="Final doctor")

        with patch("credoweb_scraper.time.monotonic", side_effect=lambda: clock[0]):
            result = self.run_main(discover=discover, fetch=fetch)
        self.assertEqual(result, 0)
        self.assertEqual(seen, ["startup", "directory", "periodic"])
        final = read_csv(self.merge)
        self.assertEqual(len(final), 2)
        self.assertEqual({row["record_status"] for row in final}, {"complete"})
        self.assertEqual({row["name"] for row in final}, {"Final doctor"})
        manifest = json.loads((self.merge / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["collection_status"], "complete")

    def test_directory_only_and_export_only_publish_csv_without_detail_requests(self):
        result = self.run_main(fetch=lambda *args, **kwargs: self.fail("Unexpected detail request"),
                               extra=("--directory-only",))
        self.assertEqual(result, 0)
        rows = read_csv(self.merge)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["record_status"] for row in rows}, {"listed"})
        with patch("credoweb_discovery.discover") as discover, \
             patch("credoweb_profiles.fetch_profile") as fetch, \
             patch("credoweb_export.export_records", return_value={}):
            result = main(["--output", str(self.output), "--export-only"])
        self.assertEqual(result, 0)
        discover.assert_not_called()
        fetch.assert_not_called()
        self.assertEqual(read_csv(self.merge), rows)

    def test_html_export_failure_does_not_prevent_clean_csv_publication(self):
        def locked_report(*args, **kwargs):
            raise PermissionError("HTML report is locked")

        result = self.run_main(report=locked_report)
        self.assertEqual(result, 1)
        rows = read_csv(self.merge)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["record_status"] for row in rows}, {"complete"})
        self.assertTrue((self.merge / "manifest.json").exists())

    def test_transient_merge_export_failure_is_retried_and_final_data_is_published(self):
        original = credoweb_merge.export_merge_records
        attempts = []

        def temporarily_locked(records, output, **kwargs):
            attempts.append(kwargs.get("collection_status"))
            if len(attempts) == 1:
                raise PermissionError("CSV temporarily locked")
            return original(records, output, **kwargs)

        with patch("credoweb_merge.export_merge_records", side_effect=temporarily_locked):
            result = self.run_main()
        self.assertEqual(result, 0)
        self.assertGreaterEqual(len(attempts), 3)
        self.assertEqual(attempts[-1], "complete")
        self.assertEqual({row["record_status"] for row in read_csv(self.merge)}, {"complete"})


if __name__ == "__main__":
    unittest.main()
