import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from credoweb_scraper import APIError, Client, arguments, selected_listings


class Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append(request)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return io.BytesIO(json.dumps(value).encode())


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = Client(Path(self.directory.name) / "test.sqlite3", delay=0)
        self.addCleanup(self.client.close)

    def test_cache_survives_restart_and_never_persists_response_token(self):
        data = {"title": "Лекар", "phone": "0888123456"}
        self.client.opener = Opener([{"data": data, "token": "do-not-save-me"}])
        self.assertEqual(self.client.get("profile/1/businessCard"), data)
        self.assertEqual(self.client.get("profile/1/businessCard"), data)
        self.assertEqual(len(self.client.opener.calls), 1)
        cached = self.client.db.execute("SELECT data FROM responses").fetchone()[0]
        self.assertNotIn("do-not-save-me", cached)
        other = Client(Path(self.directory.name) / "test.sqlite3", delay=0)
        try:
            other.opener = Opener([])
            self.assertEqual(other.get("profile/1/businessCard"), data)
            self.assertEqual(other.cache_hits, 1)
        finally:
            other.close()

    def test_authentication_failures_are_not_retried_or_cached(self):
        self.client.opener = Opener([HTTPError("url", 403, "Forbidden", {}, None)])
        with self.assertRaises(APIError) as error:
            self.client.get("profile/1/businessCard")
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(len(self.client.opener.calls), 1)
        self.assertEqual(self.client.db.execute("SELECT COUNT(*) FROM responses").fetchone()[0], 0)

    @patch("credoweb_scraper.time.sleep")
    def test_rate_limit_retries_follow_retry_after(self, sleep):
        self.client.opener = Opener([
            HTTPError("url", 429, "Too many requests", {"Retry-After": "7"}, None),
            {"data": {"result": []}},
        ])
        self.assertEqual(self.client.get("search"), {"result": []})
        sleep.assert_any_call(7.0)
        self.assertEqual(len(self.client.opener.calls), 2)

    def test_only_https_credoweb_api_urls_are_accepted(self):
        for route in ("https://attacker.example/api/search", "//attacker.example/api/search",
                      "http://www.credoweb.bg/api/search", "/settings", "../settings"):
            with self.assertRaises(APIError):
                self.client.url(route)
        self.assertEqual(self.client.url("search?cat=101", {"page": 2}),
                         "https://www.credoweb.bg/api/search?cat=101&context=bg&page=2")

    def test_profiles_are_deduplicated_and_sample_includes_both_categories(self):
        for category, profile_id in ((101, 1), (101, 2), (103, 3), (103, 4), (101, 1)):
            self.client.add_listing(category, {"profileId": profile_id})
        selected = list(selected_listings(self.client, [101, 103], 3))
        self.assertEqual([(cat, item["profileId"]) for cat, item in selected], [(101, 1), (103, 3), (101, 2)])
        self.assertEqual(self.client.db.execute("SELECT COUNT(*) FROM listings").fetchone()[0], 4)

    def test_all_directory_rows_export_before_any_profile_is_enriched(self):
        for profile_id in (1, 2, 3):
            self.client.add_listing(101, {"profileId": profile_id,
                "basicInfo": {"title": f"Doctor {profile_id}", "slug": f"doctor-{profile_id}"}})
        records = list(self.client.records())
        self.assertEqual(self.client.db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0], 0)
        self.assertEqual([record["profile_id"] for record in records], [1, 2, 3])
        self.assertEqual([record["status"] for record in records], ["listed"] * 3)
        self.assertTrue(all(record["sections"] == {} for record in records))
        self.assertEqual(records[0]["url"], "https://www.credoweb.bg/profile/1/doctor-1")

    def test_profile_checkpoints_overlay_latest_directory_without_duplicate_rows(self):
        listing = {"profileId": 1, "basicInfo": {"title": "Old title"}}
        self.client.add_listing(101, listing)
        record = {"profile_id": 1, "category": 101, "listing": listing,
                  "status": "in_progress", "sections": {"tabContacts": {"phones": ["0123456789"]}}}
        self.client.save_profile(record)
        new_listing = {"profileId": 1, "basicInfo": {"title": "Updated title"}}
        self.client.add_listing(101, new_listing)
        exported = list(self.client.records())
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]["status"], "in_progress")
        self.assertEqual(exported[0]["listing"], new_listing)
        self.assertEqual(exported[0]["sections"]["tabContacts"]["phones"], ["0123456789"])


if __name__ == "__main__":
    unittest.main()
