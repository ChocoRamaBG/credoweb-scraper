"""Detailed publication must preserve evidence and reject mixed snapshots."""
import csv
import gzip
import hashlib
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from credoweb_export import export_records
from credoweb_full import (
    FULL_FILES, SEAL_FILENAME, prepare_full_bundle, seal_full_exports, validate_full_bundle,
)
from credoweb_merge import export_merge_records
from credoweb_scraper import Client, main
from scripts import github_sync


def record(profile_id=123):
    return {
        "profile_id": profile_id, "category": 101, "status": "complete", "errors": [],
        "url": f"https://www.credoweb.bg/profile/{profile_id}/example", "fetched_at": "2026-09-29T10:00:00Z",
        "listing": {"profileId": profile_id, "profileType": {"id": 1, "label": "Лекар"}},
        "sections": {
            "businessCard": {"title": "Д-р Пример", "email": "office@example.bg"},
            "about": {"description": "Detailed biography", "workplaceList": [{
                "institution": {"id": 456, "label": "МЦ Пример"}, "currentWork": True,
                "location": {"address": "ул. Пример 7", "city": {"label": "София"},
                             "coordinates": {"latitude": 42.6977, "longitude": 23.3219}},
            }]},
        },
    }


class DetailedPublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.normalized = self.output / "merge"
        self.export()

    def export(self, records=None, **kwargs):
        records = records if records is not None else [record()]
        export_merge_records(records, self.normalized, **kwargs)
        export_records(records, self.output, include_details=False)
        seal_full_exports(self.output, self.normalized)

    def test_deterministic_gzip_preserves_exact_raw_bytes_and_coordinates(self):
        first = prepare_full_bundle(self.output, self.normalized)
        original = {name: (self.normalized / name).read_bytes() for name in FULL_FILES}
        second = prepare_full_bundle(self.output, self.normalized)
        self.assertEqual(first, second)
        self.assertEqual(original, {name: (self.normalized / name).read_bytes() for name in FULL_FILES})
        for name in ("profiles", "workplaces"):
            compressed = self.normalized / "full" / (name + ".csv.gz")
            self.assertEqual(gzip.decompress(compressed.read_bytes()), (self.output / (name + ".csv")).read_bytes())
            self.assertEqual(compressed.read_bytes()[4:8], b"\0\0\0\0")
        with gzip.open(self.normalized / "full/workplaces.csv.gz", "rt", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter=";"))
        evidence = next(json.loads(row["JSON запис"]) for row in rows if "coordinates" in row["JSON запис"])
        self.assertEqual(evidence["location"]["coordinates"], {"latitude": 42.6977, "longitude": 23.3219})
        self.assertEqual(first["normalized_manifest_sha256"], hashlib.sha256((self.normalized / "manifest.json").read_bytes()).hexdigest())
        github_sync.validate_bundle(self.normalized, require_full=True)

    def test_missing_seal_cannot_silently_publish_old_raw_exports(self):
        (self.output / SEAL_FILENAME).unlink()
        with self.assertRaisesRegex(ValueError, "seal is missing"):
            prepare_full_bundle(self.output, self.normalized)

    def test_facility_page_urls_roundtrip_with_physician_profiles_and_workplaces(self):
        facility = record(456)
        facility.update(category=103, url="https://www.credoweb.bg/page/456/medical-centre")
        self.export([record(), facility])
        manifest = prepare_full_bundle(self.output, self.normalized)
        self.assertEqual(manifest["counts"]["profiles"], 2)
        github_sync.validate_bundle(self.normalized, require_full=True)
        with gzip.open(self.normalized / "full/workplaces.csv.gz", "rt", encoding="utf-8-sig", newline="") as stream:
            source_urls = {row["Източник профил"] for row in csv.DictReader(stream, delimiter=";")}
        self.assertIn(facility["url"], source_urls)

    def test_facility_url_still_requires_matching_id_and_real_source_host(self):
        for source_url, error in (("https://www.credoweb.bg/page/999/medical-centre", "ID/source URL mismatch"),
                                  ("https://credoweb.bg.example/page/456/medical-centre", "invalid profile source URL"),
                                  ("https://www.credoweb.bg/article/456/medical-centre", "invalid profile source URL")):
            facility = record(456)
            facility.update(category=103, url=source_url)
            self.export([facility])
            with self.subTest(url=source_url), self.assertRaisesRegex(ValueError, error):
                prepare_full_bundle(self.output, self.normalized)

    def test_new_normalized_snapshot_with_same_ids_rejects_stale_raw_exports(self):
        newer = record()
        newer["sections"]["businessCard"]["email"] = "changed@example.bg"
        export_merge_records([newer], self.normalized)
        with self.assertRaisesRegex(ValueError, "seal does not match"):
            prepare_full_bundle(self.output, self.normalized)

    def test_new_raw_workplace_with_same_ids_rejects_stale_seal_and_retains_full_snapshot(self):
        prepare_full_bundle(self.output, self.normalized)
        before = {name: (self.normalized / name).read_bytes() for name in FULL_FILES}
        with (self.output / "workplaces.csv").open("ab") as stream:
            stream.write(b"altered")
        with self.assertRaisesRegex(ValueError, "differs from its snapshot seal"):
            prepare_full_bundle(self.output, self.normalized)
        self.assertEqual(before, {name: (self.normalized / name).read_bytes() for name in FULL_FILES})

    def test_running_collection_is_not_publishable(self):
        self.export(collection_status="running")
        with self.assertRaisesRegex(ValueError, "Stop collection"):
            prepare_full_bundle(self.output, self.normalized)

    def test_corrupt_gzip_or_wrong_parent_manifest_is_rejected(self):
        prepare_full_bundle(self.output, self.normalized)
        path = self.normalized / "full/workplaces.csv.gz"
        path.write_bytes(path.read_bytes() + b"bad")
        with self.assertRaisesRegex(ValueError, "compressed hash/size"):
            validate_full_bundle(self.normalized)
        prepare_full_bundle(self.output, self.normalized)
        parent = self.normalized / "manifest.json"
        parent.write_bytes(parent.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "different normalized snapshot"):
            validate_full_bundle(self.normalized)

    def test_wrong_uncompressed_hash_or_row_count_is_rejected(self):
        for field, value, message in (("uncompressed_sha256", "0" * 64, "uncompressed hash/size"),
                                       ("rows", 99, "row count")):
            prepare_full_bundle(self.output, self.normalized)
            path = self.normalized / "full/manifest.json"
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["files"]["profiles"][field] = value
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, message):
                validate_full_bundle(self.normalized)

    def test_detailed_profile_loss_is_rejected_even_after_resealing(self):
        export_merge_records([record(), record(789)], self.normalized)
        seal_full_exports(self.output, self.normalized)
        with self.assertRaisesRegex(ValueError, "IDs do not match"):
            prepare_full_bundle(self.output, self.normalized)

    def test_orphan_workplace_is_rejected_even_after_resealing(self):
        path = self.output / "workplaces.csv"
        text = path.read_text(encoding="utf-8-sig")
        text = text.replace("123;", "999;").replace("/profile/123/", "/profile/999/")
        path.write_text(text, encoding="utf-8-sig")
        seal_full_exports(self.output, self.normalized)
        with self.assertRaisesRegex(ValueError, "Unknown parent"):
            prepare_full_bundle(self.output, self.normalized)

    def test_offline_prepare_command_does_not_contact_github(self):
        with patch.object(github_sync, "run", side_effect=AssertionError("No git/network during prepare")):
            self.assertEqual(github_sync.main(["prepare", "--output", str(self.output)]), 0)
        validate_full_bundle(self.normalized)

    def test_real_export_only_collector_seals_both_formats(self):
        client = Client(self.output / "checkpoint.sqlite3", delay=0, retries=0)
        client.add_listing(101, {"profileId": 123, "basicInfo": {"title": "Д-р Пример"}})
        client.save_profile(record())
        client.close()
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Export only is offline")), \
                patch("credoweb_scraper.logging.FileHandler", return_value=logging.NullHandler()):
            self.assertIn(main(["--output", str(self.output), "--export-only", "--quick-export"]), (0, 2))
        prepare_full_bundle(self.output, self.normalized)
        validate_full_bundle(self.normalized)

    def test_failed_export_invalidates_old_seal_even_if_normalized_bytes_are_unchanged(self):
        client = Client(self.output / "checkpoint.sqlite3", delay=0, retries=0)
        client.add_listing(101, {"profileId": 123, "basicInfo": {"title": "Д-р Пример"}})
        client.save_profile(record())
        client.close()
        self.assertTrue((self.output / SEAL_FILENAME).exists())
        with patch("credoweb_export.export_records", side_effect=OSError("CSV locked")), \
                patch("credoweb_merge.export_merge_records", return_value={}), \
                patch("credoweb_scraper.logging.FileHandler", return_value=logging.NullHandler()):
            self.assertEqual(main(["--output", str(self.output), "--export-only", "--quick-export"]), 1)
        self.assertFalse((self.output / SEAL_FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
