import copy
import unittest

from credoweb_discovery import discover


def page(ids, total, pages, filters=None):
    return {"result": [{"profileId": i} for i in ids], "totalCount": total,
            "pageCount": pages, "filterList": filters or []}


class FakeClient:
    def __init__(self, responses, dropdowns=None):
        self.responses = responses
        self.dropdowns = dropdowns or {}
        self.calls = []

    def get(self, route, params):
        self.calls.append((route, params.copy()))
        if route == "dropdown":
            return self.dropdowns.get(params["key"], [])
        key = (params.get("profileType"), params.get("gender"), params.get("location"), params["page"])
        value = self.responses[key]
        if isinstance(value, Exception):
            raise value
        return value


class DiscoveryTests(unittest.TestCase):
    def test_api_is_zero_based_including_last_short_page(self):
        client = FakeClient({(None, None, None, 0): page([1, 2], 3, 2),
                             (None, None, None, 1): page([3], 3, 2)})
        items = []
        report = discover(client, [103], on_item=lambda cat, item: items.append(item["profileId"]))
        self.assertEqual(items, [1, 2, 3])
        self.assertTrue(report["complete"])
        self.assertEqual([params["page"] for _, params in client.calls], [0, 1])

    def test_demo_limit_has_honest_partial_report(self):
        client = FakeClient({(None, None, None, 0): page([1, 2], 100, 50)})
        report = discover(client, [101], max_pages=1)
        self.assertFalse(report["complete"])
        self.assertEqual(report["categories"][0]["missing"], 98)
        self.assertEqual(len(client.calls), 1)

    def test_capped_search_uses_public_type_partitions_and_deduplicates(self):
        filters = [{"key": "profileType", "options": [{"id": 13}, {"id": 28}],
                    "optionAggregationCount": {"13": 3, "28": 2}}]
        client = FakeClient({
            (None, None, None, 0): page([1, 2], 5, 2, filters),
            ("28", None, None, 0): page([4, 5], 2, 1),
            ("13", None, None, 0): page([1, 2], 3, 2),
            ("13", None, None, 1): page([3], 3, 2),
        })
        items = []
        report = discover(client, [101], on_item=lambda cat, item: items.append(item["profileId"]))
        self.assertTrue(report["complete"])
        self.assertEqual(sorted(items), [1, 2, 3, 4, 5])
        self.assertEqual(report["discovered"], 5)

    def test_repeated_page_stops_and_reports_gap(self):
        client = FakeClient({(None, None, None, 0): page([1, 2], 6, 3),
                             (None, None, None, 1): page([1, 2], 6, 3)})
        report = discover(client, [103])
        self.assertFalse(report["complete"])
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(any("repeated" in w for w in report["warnings"]))

    def test_regions_and_original_page_cover_missing_location(self):
        filters = [{"key": "location", "optionAggregationCount": {"1010": 3, "2020": 2}}]
        client = FakeClient({
            (None, None, None, 0): page([1, 6], 6, 2, filters),
            (None, None, "1010", 0): page([1, 2], 3, 2),
            (None, None, "1010", 1): page([3], 3, 2),
            (None, None, "2020", 0): page([4, 5], 2, 1),
        }, {"applicableLocations": [
            {"id": 1010, "label": "Област Първа"},
            {"id": 2020, "label": "Област Втора"},
        ]})
        report = discover(client, [101])
        self.assertTrue(report["complete"])
        self.assertEqual(report["discovered"], 6)
        self.assertFalse(any(p.get("page") == 1 and not p.get("location") for _, p in client.calls))

    def test_failed_page_does_not_claim_completeness(self):
        client = FakeClient({(None, None, None, 0): page([1, 2], 5, 3),
                             (None, None, None, 1): RuntimeError("HTTP 500"),
                             (None, None, None, 2): page([5], 5, 3)})
        report = discover(client, [103])
        self.assertFalse(report["complete"])
        self.assertEqual(report["discovered"], 3)
        self.assertEqual(len(client.calls), 3)

    def test_null_gender_not_treated_as_supported_filter(self):
        filters = [{"key": "gender", "options": [{"id": 9}, {"id": 10}],
                    "optionAggregationCount": {"9": 2, "10": 2, "0": 1}}]
        client = FakeClient({
            (None, None, None, 0): page([1, 2], 5, 2, filters),
            (None, None, None, 1): page([3, 4], 5, 2, filters),
            (None, "9", None, 0): page([1, 3], 2, 1),
            (None, "10", None, 0): page([2, 4], 2, 1),
        })
        report = discover(client, [101])
        self.assertFalse(report["complete"])
        self.assertEqual(report["categories"][0]["missing"], 1)
        self.assertFalse(any(p.get("gender") == "0" for _, p in client.calls))

    def test_persistence_callback_failure_propagates(self):
        client = FakeClient({(None, None, None, 0): page([1], 1, 1)})
        def fail(category, item):
            raise RuntimeError("disk is full")
        with self.assertRaisesRegex(RuntimeError, "disk is full"):
            discover(client, [103], on_item=fail)

    def test_missing_counts_are_an_error_not_complete_empty_catalog(self):
        client = FakeClient({(None, None, None, 0): {"result": []}})
        report = discover(client, [103])
        self.assertFalse(report["complete"])
        self.assertTrue(any("invalid totalCount" in w for w in report["warnings"]))

    def test_positive_count_without_results_is_not_complete(self):
        client = FakeClient({(None, None, None, 0): page([], 5, 1)})
        report = discover(client, [103])
        self.assertFalse(report["complete"])
        self.assertTrue(any("positive count" in w for w in report["warnings"]))

    def test_result_from_another_category_cannot_fill_coverage_gap(self):
        response = page([1, 2], 2, 1)
        response["result"][1]["categoryId"] = 101
        client = FakeClient({(None, None, None, 0): response})
        report = discover(client, [103])
        self.assertFalse(report["complete"])
        self.assertEqual(report["discovered"], 1)

    def test_checkpoint_has_category_metadata_before_page_traversal(self):
        client = FakeClient({(None, None, None, 0): page([1, 2], 3, 2),
                             (None, None, None, 1): page([3], 3, 2)})
        snapshots = []
        report = discover(client, [103], on_checkpoint=lambda r: snapshots.append(copy.deepcopy(r)))
        self.assertEqual(snapshots[0]["categories"][0]["status"], "pending")
        self.assertTrue(any(c["expected_known"] and c["expected"] == 3 and c["discovered"] == 0
                            for r in snapshots for c in r["categories"]))
        self.assertTrue(any(r["discovered"] == 2 for r in snapshots))
        self.assertFalse(report["running"])

    def test_type_partition_gap_does_not_trigger_parent_backtracking(self):
        filters = [{"key": "profileType", "options": [{"id": 13}, {"id": 28}],
                    "optionAggregationCount": {"13": 3, "28": 2}}]
        client = FakeClient({
            (None, None, None, 0): page([1, 2], 5, 2, filters),
            ("28", None, None, 0): page([4, 5], 2, 1),
            ("13", None, None, 0): page([1, 2], 3, 2),
            ("13", None, None, 1): page([1], 3, 2),
        })
        report = discover(client, [101])
        self.assertFalse(report["complete"])
        self.assertEqual(report["discovered"], 4)
        self.assertEqual(len(client.calls), 4)

    def test_zero_recovery_budget_preserves_existing_listings(self):
        client = FakeClient({(None, None, None, 0): page([1, 2], 5, 2)})
        received = []
        report = discover(client, [101], max_recovery_requests=0,
                          initial_items=[(101, {"profileId": 4})],
                          on_item=lambda cat, item: received.append(item["profileId"]))
        self.assertEqual(sorted(received), [1, 2, 4])
        self.assertEqual(report["discovered"], 3)
        self.assertTrue(report["categories"][0]["recovery_budget_exhausted"])
        self.assertFalse(report["complete"])

    def test_cached_recovery_pages_do_not_consume_network_budget(self):
        filters = [{"key": "gender", "options": [{"id": 9}, {"id": 10}],
                    "optionAggregationCount": {"9": 2, "10": 2, "0": 1}}]
        class CachedClient(FakeClient):
            requests = 0
            def get_cached(self, route, params):
                if params.get("page") == 1 and not params.get("gender"):
                    return page([3, 4], 5, 2)
                return None
            def get(self, route, params):
                if route == "search":
                    self.requests += 1
                return super().get(route, params)
        client = CachedClient({
            (None, None, None, 0): page([1, 2], 5, 2, filters),
            (None, "9", None, 0): page([1, 3], 2, 1),
        })
        report = discover(client, [101], max_recovery_requests=1)
        self.assertEqual(report["discovered"], 4)
        self.assertEqual(report["categories"][0]["recovery_http_requests"], 1)
        self.assertEqual(client.requests, 2)
        self.assertTrue(report["categories"][0]["recovery_budget_exhausted"])

    def test_initial_checkpoint_includes_cached_totals_of_pending_categories(self):
        class CachedClient(FakeClient):
            def get_cached(self, route, params):
                return page([1], 3 if params["cat"] == 101 else 1, 3 if params["cat"] == 101 else 1)
        client = CachedClient({})
        snapshots = []
        def stop_at_first_checkpoint(report):
            snapshots.append(copy.deepcopy(report))
            raise RuntimeError("stop before network")
        with self.assertRaisesRegex(RuntimeError, "stop before network"):
            discover(client, [103, 101], initial_items=[(101, {"profileId": 1})],
                     on_checkpoint=stop_at_first_checkpoint)
        self.assertEqual(snapshots[0]["expected"], 4)
        self.assertEqual(snapshots[0]["discovered"], 1)
        self.assertTrue(all(c["expected_known"] for c in snapshots[0]["categories"]))
        self.assertEqual(client.calls, [])

    def test_uncapped_pagination_gap_gets_bounded_targeted_recovery(self):
        filters = [{"key": "gender", "options": [{"id": 9}, {"id": 10}],
                    "optionAggregationCount": {"9": 2, "10": 1}}]
        first = page([1, 2], 3, 2, filters)
        first["result"][0]["basicInfo"] = {"sex": {"id": 10}}
        first["result"][1]["basicInfo"] = {"sex": {"id": 9}}
        client = FakeClient({
            (None, None, None, 0): first,
            (None, None, None, 1): page([1], 3, 2),
            (None, "9", None, 0): page([2, 3], 2, 1),
        })
        report = discover(client, [101], max_recovery_requests=1)
        self.assertTrue(report["complete"])
        self.assertEqual(report["discovered"], 3)
        self.assertEqual(report["categories"][0]["recovery_http_requests"], 1)
        self.assertEqual(len(client.calls), 3)

    def test_hospital_city_deficit_precedes_unmapped_regions_and_deduplicates_addresses(self):
        filters = [{"key": "location", "optionAggregationCount": {
            "100": 2, "200": 1, "9000": 1, "23264": 3}}]
        first = page([1, 2], 3, 2, filters)
        first["result"][0]["basicInfo"] = {"city": {"id": "100"}, "country": {"id": 23264}}
        first["result"][0]["practiceList"] = [{"address": {"city": {"id": 100}}}]
        first["result"][1]["basicInfo"] = {"city": {"id": 200}, "country": {"id": 23264}}
        client = FakeClient({
            (None, None, None, 0): first,
            (None, None, None, 1): page([1], 3, 2),
            (None, None, "100", 0): page([1, 3], 2, 1),
        })
        report = discover(client, [103], max_recovery_requests=1)
        self.assertTrue(report["complete"])
        self.assertEqual(report["discovered"], 3)
        self.assertEqual(client.calls[-1][1]["location"], "100")
        self.assertEqual(report["categories"][0]["recovery_http_requests"], 1)

    def test_unseen_city_bucket_is_still_attempted_when_known_cities_are_covered(self):
        filters = [{"key": "location", "optionAggregationCount": {"100": 2, "300": 1}}]
        first = page([1, 2], 3, 2, filters)
        for item in first["result"]:
            item["basicInfo"] = {"city": {"id": 100}}
        client = FakeClient({
            (None, None, None, 0): first,
            (None, None, None, 1): page([1], 3, 2),
            (None, None, "300", 0): page([3], 1, 1),
        })
        report = discover(client, [103], max_recovery_requests=1)
        self.assertTrue(report["complete"])
        self.assertEqual(client.calls[-1][1]["location"], "300")


if __name__ == "__main__":
    unittest.main()
