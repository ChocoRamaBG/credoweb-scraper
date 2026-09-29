"""Regression tests for data preservation and untrusted profile text."""

import csv
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from credoweb_export import export_records, profile_row, workplace_rows


def record_fixture():
    practice = {
        "institution": {"id": 87, "label": "МЦ Пример", "city": {"label": "София"}},
        "location": {"address": "ул. Пример 7", "postCode": {"label": "0123"}},
        "contactList": {"phoneNumbers": ["0888123456", "+35929921252"]},
        "consultationHours": [{"day": [0, 2], "from": "09:00", "to": "12:00"}],
        "positionTitle": "Кардиолог", "currentWork": True, "practice": True,
        "insurers": [{"label": "НЗОК"}], "appointmentNeeded": False,
    }
    return {
        "profile_id": 123, "category": 101,
        "url": "https://www.credoweb.bg/profile/123/test",
        "listing": {"basicInfo": {"title": "Д-р Пример", "profileType": {"label": "Лекар"}}},
        "sections": {
            "businessCard": {"title": "Д-р Пример", "city": {"label": "София"}},
            "about": {"about": {"description": "<p>Първи ред</p><p>Втори &amp; трети</p><script>bad()</script>"}, "workplaceList": [practice]},
            "tabContacts": {"phone": "+359888111222", "phones": ["0888999888"], "contactsList": [practice]},
            "teamList?page=1": {"contentList": [{"title": "Друг човек", "phone": "DO-NOT-INCLUDE"}]},
            "unknown": {"a/b~c": [None, False, -1.25, {}, [], {"0": "=1+1"}], "empty": ""},
        },
        "fetched_at": "2026-09-28T12:00:00Z", "status": "partial",
        "errors": [{"route": "profile/123?module=tabPublicationPublished", "message": "HTTP 503"}],
    }


class ExportTests(unittest.TestCase):
    def test_facility_card_address_is_exported_before_contacts_are_fetched(self):
        card = {"profileId": 87, "title": "МЦ Пример", "city": {"label": "София"},
                "country": {"label": "България"},
                "location": {"address": "ул. София 7", "postCode": {"label": "1000"}}}
        record = {"profile_id": 87, "category": 103, "status": "in_progress",
                  "sections": {"businessCard": card}}
        summary = profile_row(record)
        self.assertEqual(summary["Адреси"], "България, София, 1000, ул. София 7")
        rows = list(workplace_rows(record, summary))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Адреси"], summary["Адреси"])
        self.assertEqual(rows[0]["Наименование"], "МЦ Пример")
        self.assertEqual(rows[0]["Източник поле"], "/sections/businessCard")
        record["sections"]["tabContacts"] = {"contactsList": card}
        rows = list(workplace_rows(record, profile_row(record)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Източник поле"], "/sections/tabContacts/contactsList")

    def test_facility_formatted_and_nested_street_address_are_one_full_address(self):
        full_address = "България, Варна, 9000, ул. Пример 7"
        record = record_fixture()
        record["category"] = 103
        record["sections"] = {
            "businessCard": {"title": "МБАЛ Пример", "city": {"label": "Варна"}},
            "tabContacts": {"contactsList": {
                "address": full_address,
                "location": {"country": {"label": "България"}, "city": {"label": "Варна"},
                             "postCode": {"label": "9000"}, "address": "ул. Пример 7"},
            }},
        }
        summary = profile_row(record)
        self.assertEqual(summary["Адреси"], full_address)
        rows = list(workplace_rows(record, summary))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Адреси"], full_address)

    def test_practice_addresses_use_each_location_city_and_keep_separate_addresses(self):
        record = record_fixture()
        practices = [
            {"institution": {"label": "МЦ Варна", "city": {"label": "Варна"}},
             "location": {"country": {"label": "България"}, "postCode": {"label": "9000"},
                          "address": "ул. Пример 7"}},
            {"institution": {"label": "МЦ Пловдив", "city": {"label": "Пловдив"}},
             "location": {"country": {"label": "България"}, "postCode": {"label": "4000"},
                          "address": "ул. Пример 7"}},
        ]
        record["sections"] = {
            "businessCard": {"title": "Д-р Пример", "city": {"label": "София"}},
            "about": {"practiceList": practices},
        }
        summary = profile_row(record)
        expected = ["България, Варна, 9000, ул. Пример 7",
                    "България, Пловдив, 4000, ул. Пример 7"]
        self.assertEqual(summary["Град"], "София")
        self.assertEqual(summary["Адреси"].splitlines(), expected)
        rows = list(workplace_rows(record, summary))
        self.assertEqual([row["Адреси"] for row in rows], expected)
        self.assertEqual([row["Град"] for row in rows], ["Варна", "Пловдив"])

    def test_partial_about_address_is_deduplicated_before_or_after_full_address(self):
        full_address = "България, Варна, 9000, ул. Пример 7"
        for first, second in (("ул. Пример 7", full_address),
                              (full_address, "ул. Пример 7")):
            with self.subTest(first=first):
                record = record_fixture()
                record["sections"] = {
                    "businessCard": {"title": "МБАЛ Пример", "address": first},
                    "about": {"address": second},
                    "tabContacts": {"address": "ул. Пример 7"},
                }
                self.assertEqual(profile_row(record)["Адреси"], full_address)

    def test_sparse_location_does_not_invent_a_street_address(self):
        record = record_fixture()
        record["sections"] = {
            "businessCard": {"title": "Д-р Пример", "city": {"label": "София"},
                             "country": {"label": "България"}},
            "about": {"practiceList": [{
                "institution": {"label": "МЦ Варна", "city": {"label": "Варна"}},
                "location": {"postCode": {"label": "9000"}, "address": ""},
            }]},
        }
        summary = profile_row(record)
        self.assertEqual(summary["Адреси"], "")
        rows = list(workplace_rows(record, summary))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Адреси"], "")

    def test_team_members_addresses_are_excluded_from_profile_address(self):
        record = record_fixture()
        record["sections"] = {
            "businessCard": {"title": "МБАЛ Пример", "address": "бул. Болница 1"},
            "teamList?page=1": {"contentList": [{
                "title": "Друг лекар", "address": "ул. Чужд адрес 99",
                "practiceList": [{"location": {"address": "ул. Друга практика 88"}}],
            }]},
        }
        self.assertEqual(profile_row(record)["Адреси"], "бул. Болница 1")

    def test_main_html_table_has_full_address_and_separate_phone_email_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            export_records([record_fixture()], directory, include_details=False)
            content = (Path(directory) / "report.html").read_text(encoding="utf-8")
            header = content.split("<thead>", 1)[1].split("</thead>", 1)[0]
            self.assertIn("<th>Пълен адрес</th><th>Телефон</th><th>Имейл</th>", header)
            self.assertNotIn("<th>Контакти</th>", header)

    def test_rich_contacts_identity_and_multiple_workplaces(self):
        record = record_fixture()
        summary = profile_row(record)
        self.assertEqual(summary["Име"], "Д-р Пример")
        self.assertEqual(summary["Тип профил"], "Лекар")
        for phone in ("0888123456", "+35929921252", "+359888111222", "0888999888"):
            self.assertIn(phone, summary["Телефони"])
        self.assertNotIn("DO-NOT-INCLUDE", summary["Телефони"])
        self.assertEqual(summary["Описание"], "Първи ред\nВтори & трети")
        rows = list(workplace_rows(record, summary))
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["Източник поле"], rows[1]["Източник поле"])
        self.assertEqual(rows[0]["Наименование"], "МЦ Пример")
        self.assertEqual(rows[0]["ID място"], 87)
        self.assertEqual(rows[0]["Длъжност"], "Кардиолог")
        self.assertEqual(rows[0]["С предварително записване"], "Не")
        self.assertEqual(json.loads(rows[0]["Работно време"])[0]["day"], [0, 2])

    def test_details_can_reconstruct_complete_json_and_csv_safety(self):
        record = record_fixture()
        record["sections"]["businessCard"]["title"] = "  =HYPERLINK(\"https://example.com\")"
        with tempfile.TemporaryDirectory() as directory:
            result = export_records(iter([record]), Path(directory))
            self.assertEqual(result["counts"]["profiles"], 1)
            self.assertEqual(result["counts"]["errors"], 1)
            path = Path(directory) / "profiles.csv"
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
            with path.open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle, delimiter=";"))
            self.assertTrue(row["Име"].startswith("'"))
            self.assertIn("0888123456", row["Телефони"])
            with (Path(directory) / "details.csv").open(encoding="utf-8-sig", newline="") as handle:
                details = list(csv.DictReader(handle, delimiter=";"))
            root = None
            for detail in details:
                path = detail["Път JSON Pointer"]
                value = json.loads(detail["JSON стойност"])
                if path == "":
                    root = value
                    continue
                parts = [part.replace("~1", "/").replace("~0", "~") for part in path.split("/")[1:]]
                parent = root
                for part in parts[:-1]:
                    parent = parent[int(part)] if isinstance(parent, list) else parent[part]
                if isinstance(parent, list):
                    self.assertEqual(len(parent), int(parts[-1]))
                    parent.append(value)
                else:
                    parent[parts[-1]] = value
            self.assertEqual(root, record)
            with (Path(directory) / "errors.csv").open(encoding="utf-8-sig", newline="") as handle:
                error = next(csv.DictReader(handle, delimiter=";"))
            self.assertEqual(error["Секция"], record["errors"][0]["route"])

    def test_embedded_html_is_inert_and_keeps_summary_text(self):
        record = record_fixture()
        payload = '</script><script>alert("x")</script><img src=x onerror=alert(1)>'
        record["url"] = payload
        with tempfile.TemporaryDirectory() as directory:
            export_records([record], directory)
            content = (Path(directory) / "report.html").read_text(encoding="utf-8")
            self.assertNotIn(payload, content)
            self.assertEqual(content.count("</script>"), 2)
            encoded = content.split('<script id="records" type="application/json">', 1)[1].split("</script>", 1)[0]
            self.assertEqual(json.loads(encoded)[0]["p"]["Източник"], payload)
            self.assertNotIn("innerHTML", content)

    def test_empty_export_has_headers_and_valid_html_data(self):
        with tempfile.TemporaryDirectory() as directory:
            result = export_records(iter([]), directory)
            self.assertEqual(result["counts"], {"profiles": 0, "workplaces": 0, "details": 0, "errors": 0})
            with (Path(directory) / "profiles.csv").open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle, delimiter=";")
                self.assertIn("Име", reader.fieldnames)
                self.assertEqual(list(reader), [])

    def test_manifest_coverage_labels_sample_and_preserves_interrupted_status(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            manifest = {"status": "sample", "limits": {"max_profiles": 6}, "discovery": {"expected": 25622}}
            for status in ("sample", "interrupted", "failed"):
                manifest["status"] = status
                (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                export_records([record_fixture()], folder)
                html = (folder / "report.html").read_text(encoding="utf-8")
                metadata = json.loads(html.split("const run = ", 1)[1].split(";", 1)[0])
                self.assertEqual(metadata["status"], status)
                self.assertEqual(metadata["expected"], 25622)
                self.assertIn("Ограничена извадка", html)
                self.assertNotIn("__RUN_METADATA__", html)

    def test_no_manifest_keeps_generic_report_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            export_records([record_fixture()], directory)
            html = (Path(directory) / "report.html").read_text(encoding="utf-8")
            metadata = json.loads(html.split("const run = ", 1)[1].split(";", 1)[0])
            self.assertEqual(metadata["status"], "")
            self.assertIsNone(metadata["expected"])

    def test_quick_export_preserves_full_files_and_skips_raw_traversal(self):
        record = record_fixture()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "details.csv").write_bytes(b"previous complete details")
            (folder / "errors.csv").write_bytes(b"previous complete errors")
            with patch("credoweb_export.flatten_json", side_effect=AssertionError("Quick export must not traverse raw JSON")):
                result = export_records([record], folder, include_details=False)
            self.assertEqual((folder / "details.csv").read_bytes(), b"previous complete details")
            self.assertEqual((folder / "errors.csv").read_bytes(), b"previous complete errors")
            self.assertIsNone(result["counts"]["details"])
            self.assertEqual(result["counts"]["errors"], 1)
            self.assertEqual(set(result["files"]), {"profiles.csv", "workplaces.csv", "report.html"})
            content = (folder / "report.html").read_text(encoding="utf-8")
            self.assertNotIn('href="details.csv"', content)
            self.assertNotIn('href="errors.csv"', content)
            metadata = json.loads(content.split("const run = ", 1)[1].split(";", 1)[0])
            self.assertFalse(metadata["details_current"])

    def test_progressive_report_counts_catalogue_and_enriched_profiles_separately(self):
        listed = record_fixture()
        listed.update({"status": "listed", "sections": {}, "errors": []})
        enriched = record_fixture()
        enriched.update({"profile_id": 456, "status": "in_progress"})
        hospital = {"profile_id": 789, "category": 103, "status": "complete", "listing": {"basicInfo": {"title": "Болница", "profileType": {"label": "Лечебно заведение"}}}}
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            manifest = {"status": "running", "stage": "enrich", "discovery": {"categories": [{"category": 101, "expected": 23417, "discovered": 23342}, {"category": 103, "expected": 2205, "discovered": 2205}]}}
            (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            export_records([listed, enriched, hospital], folder, include_details=False)
            content = (folder / "report.html").read_text(encoding="utf-8")
            metadata = json.loads(content.split("const run = ", 1)[1].split(";", 1)[0])
            self.assertEqual(metadata["profile_statuses"], {"listed": 1, "in_progress": 1, "complete": 1})
            self.assertEqual(metadata["stage"], "enrich")
            self.assertTrue(metadata["running"])
            self.assertEqual(metadata["expected"], 25622)
            self.assertEqual(metadata["categories"][0]["exported"], 2)
            self.assertEqual(metadata["categories"][0]["discovered"], 23342)
            self.assertEqual(metadata["categories"][1]["statuses"], {"complete": 1})
            self.assertIn('id="profile-type"', content)
            with (folder / "profiles.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle, delimiter=";"))
            self.assertEqual(rows[0]["Име"], "Д-р Пример")
            self.assertEqual(rows[0]["Статус"], "listed")


if __name__ == "__main__":
    unittest.main()
