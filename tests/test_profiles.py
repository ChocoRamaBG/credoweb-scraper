"""Coverage checks for public profile discovery and pagination boundaries."""
import copy
import unittest

from credoweb_profiles import fetch_profile


class APIError(RuntimeError):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, route, params=None):
        key = (route, tuple(sorted((params or {}).items())))
        self.calls.append(key)
        value = self.responses[key]
        if isinstance(value, BaseException):
            raise value
        return value


def key(module=None, **params):
    if module:
        params["module"] = module
    return ("profile/123", tuple(sorted(params.items())))


class ProfileTests(unittest.TestCase):
    listing = {"profileId": 123, "profileType": "page", "basicInfo": {"slug": "clinic"}}

    def client(self, navigation, extra=None):
        responses = {
            ("profile/123/businessCard", ()): {"title": "Clinic"},
            key("subNavigation"): {"navigation": navigation},
        }
        responses.update(extra or {})
        return FakeClient(responses)

    def test_nested_tabs_are_deduplicated_and_all_page_contracts_work(self):
        client = self.client([
            {"backendRoute": "profile/123?module=teamList", "subTabs": [
                {"backendRoute": "profile/123?module=teamList"},
                {"backendRoute": "profile/123?module=tabRelated"},
            ]},
        ], {
            key("teamList"): {"contentList": [{"profileId": 5}], "pagesCount": 2, "page": 1, "isLastPage": False},
            key("teamList", page=1): {"contentList": [{"profileId": 6}], "pagesCount": 2, "page": 2, "isLastPage": True},
            key("tabRelated"): {"relatedPagesList": [{"profileId": 7}], "pagesCount": 2},
            key("tabRelated", page=1): {"relatedPagesList": [{"profileId": 8}], "pagesCount": 2},
        })
        record = fetch_profile(client, self.listing, 103)
        self.assertEqual(record["status"], "complete")
        self.assertIn("teamList?page=1", record["sections"])
        self.assertIn("tabRelated?page=1", record["sections"])
        self.assertEqual(client.calls.count(key("teamList")), 1)
        self.assertEqual(len(client.calls), 6)

    def test_only_advertised_entry_details_are_fetched(self):
        client = self.client([{"backendRoute": "profile/123?module=structureList"}], {
            key("structureList"): {"structureList": {"profileId": 123, "children": [
                {"profileId": 9, "profileType": {"type": "entry"}},
                {"profileId": 10, "profileType": {"type": "page"}},
            ]}},
            key("structure", entryId=9): {"profileId": 9, "contactList": {"phoneList": []}},
        })
        record = fetch_profile(client, self.listing, 103)
        self.assertEqual(record["status"], "complete")
        self.assertIn("structure?entryId=9", record["sections"])
        self.assertEqual(len(client.calls), 4)

    def test_access_failure_is_recorded_without_credentials_or_fallback(self):
        client = self.client([{"backendRoute": "profile/123?module=tabContacts"}], {
            key("tabContacts"): APIError(403),
        })
        record = fetch_profile(client, self.listing, 103)
        self.assertEqual(record["status"], "partial")
        self.assertEqual(record["errors"][0]["kind"], "access_denied")
        self.assertEqual(record["errors"][0]["status"], 403)
        self.assertEqual(len(client.calls), 3)

    def test_external_and_other_profile_routes_are_never_requested(self):
        client = self.client([
            {"backendRoute": "https://example.org/api/profile/123?module=about"},
            {"backendRoute": "profile/456?module=about"},
            {"backendRoute": "profile/123?module=settings"},
        ])
        record = fetch_profile(client, self.listing, 103)
        self.assertEqual(record["status"], "partial")
        self.assertEqual(len(record["errors"]), 3)
        self.assertEqual(len(client.calls), 2)

    def test_page_limit_and_repeated_results_never_claim_completeness(self):
        first = {"contentList": [{"id": 1}], "pageCount": 3, "isLastPage": False}
        client = self.client([{"backendRoute": "profile/123?module=tabPublicationPublished"}], {
            key("tabPublicationPublished"): first,
            key("tabPublicationPublished", page=1): first,
        })
        capped = fetch_profile(client, self.listing, 103, max_section_pages=1)
        self.assertEqual(capped["status"], "partial")
        self.assertEqual(capped["errors"][0]["kind"], "page_limit")
        uncapped = fetch_profile(client, self.listing, 103)
        self.assertEqual(uncapped["status"], "partial")
        self.assertEqual(uncapped["errors"][0]["kind"], "repeated_page")

    def test_contact_checkpoint_survives_interruption_during_large_feed(self):
        client = self.client([
            {"backendRoute": "profile/123?module=tabPublicationPublished"},
            {"backendRoute": "profile/123?module=tabContacts"},
            {"backendRoute": "profile/123?module=about"},
        ], {
            key("about"): {"about": {"description": "Public biography"}},
            key("tabContacts"): {"phones": ["0123456789"]},
            key("tabPublicationPublished"): {"contentList": [{"id": 1}], "page": 1,
                                             "pageCount": 100, "isLastPage": False},
            key("tabPublicationPublished", page=1): KeyboardInterrupt(),
        })
        snapshots = []
        with self.assertRaises(KeyboardInterrupt):
            fetch_profile(client, self.listing, 103,
                          on_section=lambda record: snapshots.append(copy.deepcopy(record)))
        self.assertEqual(client.calls[2:5], [key("about"), key("tabContacts"), key("tabPublicationPublished")])
        self.assertEqual(snapshots[-1]["status"], "in_progress")
        self.assertEqual(snapshots[-1]["sections"]["tabContacts"]["phones"], ["0123456789"])
        self.assertIn("tabPublicationPublished", snapshots[-1]["sections"])
        self.assertTrue(all(record["status"] != "complete" for record in snapshots))

    def test_callback_records_final_status_and_failures_are_not_swallowed(self):
        client = self.client([])
        snapshots = []
        record = fetch_profile(client, self.listing, 103,
                               on_section=lambda value: snapshots.append(copy.deepcopy(value)))
        self.assertEqual([value["status"] for value in snapshots], ["in_progress", "in_progress", "complete"])
        self.assertEqual(snapshots[-1], record)
        def failed_checkpoint(record):
            raise RuntimeError("Checkpoint disk full")
        client = self.client([])
        with self.assertRaisesRegex(RuntimeError, "Checkpoint disk full"):
            fetch_profile(client, self.listing, 103, on_section=failed_checkpoint)
        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
