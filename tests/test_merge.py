"""Relational merge-export regressions using only artificial records/databases."""

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from credoweb_merge import build_tables, read_snapshot


def practice_fixture(facility_id=87, city="Варна", postcode="0123"):
    return {
        "institution": {"id": facility_id, "label": "МЦ " + city,
                        "city": {"label": city}},
        "location": {"country": {"label": "България"}, "city": {"label": city},
                     "postCode": {"label": postcode}, "address": "ул. Пример 7"},
        "contactList": {"phoneNumbers": ["0888123456", "+35929921252"],
                        "email": "Clinic@Example.bg"},
        "positionTitle": "Кардиолог", "currentWork": True, "practice": True,
    }


def merge_fixture():
    first = practice_fixture()
    second = practice_fixture(88, "Пловдив", "4000")
    return {
        "profile_id": 123, "category": 101,
        "url": "https://www.credoweb.bg/profile/123/doctor-example",
        "listing": {
            "profileId": 123,
            "basicInfo": {"title": "Д-р Пример", "slug": "doctor-example",
                          "profileType": {"id": 1, "label": "Лекар"},
                          "city": {"label": "София"}},
            "practiceList": [copy.deepcopy(first), copy.deepcopy(second)],
        },
        "sections": {
            "businessCard": {"title": "Д-р Пример", "city": {"label": "София"},
                             "profileType": {"id": 1, "label": "Лекар"},
                             "mainSpeciality": {"id": 45, "label": "Кардиология"}},
            "about": {"practiceList": [copy.deepcopy(first), copy.deepcopy(second)],
                      "otherSpeciality": [{"id": 46, "label": "Вътрешни болести"}]},
            "tabContacts": {"phone": " 02 123 45 67 ", "email": "Doctor@Example.bg",
                            "contactsList": [copy.deepcopy(first), copy.deepcopy(second)]},
        },
        "fetched_at": "2026-09-29T12:00:00Z", "status": "partial", "errors": [],
    }


class MergeTableTests(unittest.TestCase):
    def test_published_phone_country_code_and_primary_flag_are_preserved(self):
        record = {"profile_id": 19314, "category": 103, "sections": {
            "businessCard": {"title": "Болница", "contactList": {"phoneList": [
                {"number": "56894501", "phoneCodeStr": "+359", "primary": True}
            ]}},
        }}
        tables = build_tables([record])
        phone = tables["contacts"][0]
        self.assertEqual(phone["contact_value"], "56894501")
        self.assertEqual(phone["contact_normalized"], "56894501")
        self.assertEqual(phone["phone_country_code"], "+359")
        self.assertEqual(phone["is_primary"], "true")
        self.assertEqual(tables["profiles"][0]["phone_country_code"], "+359")

    def test_city_only_workplace_does_not_hide_later_street_address(self):
        record = merge_fixture()
        sparse = {"institution": {"id": 40, "label": "МЦ Без улица", "city": {"label": "София"}}}
        record["listing"]["practiceList"] = [sparse, practice_fixture()]
        record["sections"] = {}
        tables = build_tables([record])
        profile = tables["profiles"][0]
        self.assertEqual(profile["address_count"], 1)
        self.assertEqual(profile["full_address"], "България, Варна, 0123, ул. Пример 7")
        self.assertEqual(next(row for row in tables["workplaces"] if row["facility_profile_id"] == "40")["full_address"], "")

    def test_facility_branch_does_not_borrow_different_owner_city_id(self):
        record = {"profile_id": 87, "category": 103, "sections": {
            "businessCard": {"title": "Болница", "city": {"id": 1, "label": "София"},
                             "country": {"id": 3, "label": "България"}},
            "tabContacts": {"contactsList": [{"city": {"label": "Варна"},
                                             "country": {"label": "Друга държава"},
                                             "location": {"address": "ул. Пример 7"}}]},
        }}
        branch = build_tables([record])["workplaces"][0]
        self.assertEqual(branch["city"], "Варна")
        self.assertEqual(branch["city_id"], "")
        self.assertEqual(branch["country"], "Друга държава")
        self.assertEqual(branch["country_id"], "")

    def test_conflicting_explicit_relationship_flags_are_not_silently_merged(self):
        record = merge_fixture()
        current = practice_fixture()
        previous = copy.deepcopy(current)
        previous["currentWork"] = False
        record["listing"]["practiceList"] = [current, previous]
        record["sections"] = {}
        rows = build_tables([record])["workplaces"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["is_current"] for row in rows}, {"true", "false"})
        self.assertEqual(len({row["workplace_key"] for row in rows}), 2)

    def test_one_profile_has_two_workplaces_without_replacing_profile_city(self):
        tables = build_tables([merge_fixture()])
        self.assertEqual(set(tables), {"profiles", "workplaces", "contacts", "specialties"})
        self.assertEqual(len(tables["profiles"]), 1)
        profile = tables["profiles"][0]
        self.assertEqual(profile["profile_id"], "123")
        self.assertEqual(profile["profile_city"], "София")
        self.assertEqual(profile["workplace_count"], 2)
        workplaces = {row["facility_profile_id"]: row for row in tables["workplaces"]}
        self.assertEqual(set(workplaces), {"87", "88"})
        self.assertEqual(workplaces["87"]["city"], "Варна")
        self.assertEqual(workplaces["87"]["postal_code"], "0123")
        self.assertEqual(workplaces["87"]["full_address"], "България, Варна, 0123, ул. Пример 7")
        self.assertEqual(workplaces["88"]["city"], "Пловдив")
        self.assertEqual(workplaces["88"]["full_address"], "България, Пловдив, 4000, ул. Пример 7")
        self.assertEqual({row["is_current"] for row in workplaces.values()}, {"true"})

    def test_repeated_listing_about_and_contacts_workplaces_are_deduplicated(self):
        record = merge_fixture()
        repeated = build_tables([record])
        unique_record = copy.deepcopy(record)
        unique_record["listing"].pop("practiceList")
        unique_record["sections"]["tabContacts"].pop("contactsList")
        unique = build_tables([unique_record])
        self.assertEqual(len(repeated["workplaces"]), 2)
        self.assertEqual(len(repeated["contacts"]), len(unique["contacts"]))
        self.assertEqual({row["workplace_key"] for row in repeated["workplaces"]},
                         {row["workplace_key"] for row in unique["workplaces"]})
        self.assertEqual({row["contact_id"] for row in repeated["contacts"]},
                         {row["contact_id"] for row in unique["contacts"]})

    def test_phone_and_email_values_are_separate_and_keep_original_phone_text(self):
        contacts = build_tables([merge_fixture()])["contacts"]
        phones = {row["contact_value"]: row["contact_normalized"]
                  for row in contacts if row["contact_type"] == "phone"}
        emails = {row["contact_value"]: row["contact_normalized"]
                  for row in contacts if row["contact_type"] == "email"}
        self.assertEqual(phones["0888123456"], "0888123456")
        self.assertEqual(phones["+35929921252"], "+35929921252")
        self.assertEqual(phones[" 02 123 45 67 "], "021234567")
        self.assertEqual(emails["Doctor@Example.bg"], "doctor@example.bg")
        self.assertEqual(emails["Clinic@Example.bg"], "clinic@example.bg")
        self.assertFalse(set(phones) & set(emails))
        profile = build_tables([merge_fixture()])["profiles"][0]
        self.assertIn(profile["phone"], phones)
        self.assertIn(profile["email"], emails)

    def test_ambiguous_telephone_text_is_preserved_without_guessing_normalization(self):
        record = merge_fixture()
        record["sections"]["tabContacts"]["phones"] = [
            "0888123456 / 0888999888", "02 123 456, 02 987 654", "Регистратура 02 123 456"]
        contacts = build_tables([record])["contacts"]
        by_value = {row["contact_value"]: row for row in contacts}
        for value in record["sections"]["tabContacts"]["phones"]:
            with self.subTest(value=value):
                self.assertEqual(by_value[value]["contact_type"], "phone")
                self.assertEqual(by_value[value]["contact_normalized"], "")

    def test_table_keys_are_unique_and_contact_foreign_keys_match_owner(self):
        record = merge_fixture()
        second = copy.deepcopy(record)
        second["profile_id"] = 456
        second["listing"]["profileId"] = 456
        second["url"] = "https://www.credoweb.bg/profile/456/other-doctor"
        tables = build_tables([record, second])
        for table, column in (("profiles", "profile_id"), ("profiles", "entity_key"),
                              ("workplaces", "workplace_key"), ("contacts", "contact_id")):
            with self.subTest(table=table, column=column):
                values = [row[column] for row in tables[table]]
                self.assertTrue(all(isinstance(value, str) and value for value in values))
                self.assertEqual(len(values), len(set(values)))
        profile_ids = {row["profile_id"] for row in tables["profiles"]}
        workplaces = {row["workplace_key"]: row for row in tables["workplaces"]}
        for table in ("workplaces", "contacts", "specialties"):
            for row in tables[table]:
                self.assertIn(row["profile_id"], profile_ids)
        for contact in tables["contacts"]:
            if contact["workplace_key"]:
                self.assertIn(contact["workplace_key"], workplaces)
                self.assertEqual(contact["profile_id"], workplaces[contact["workplace_key"]]["profile_id"])
        specialties = {(row["profile_id"], row["specialty_kind"], row["specialty_id"], row["specialty_name"])
                       for row in tables["specialties"]}
        self.assertEqual(len(specialties), len(tables["specialties"]))
        self.assertIn(("123", "main", "45", "Кардиология"), specialties)
        self.assertIn(("123", "other", "46", "Вътрешни болести"), specialties)

    def test_staff_and_feed_contacts_and_workplaces_do_not_enter_owner_tables(self):
        record = merge_fixture()
        expected = build_tables([copy.deepcopy(record)])
        foreign_person = {"title": "Чужд човек", "phone": "0999999999", "email": "foreign@example.bg",
                          "practiceList": [practice_fixture(999, "Чужд град", "9999")]}
        record["sections"]["teamList?page=1"] = {"contentList": [copy.deepcopy(foreign_person)]}
        record["sections"]["tabPublicationPublished?page=1"] = {
            "contentList": [{"author": copy.deepcopy(foreign_person)}]}
        record["listing"]["teamList"] = [copy.deepcopy(foreign_person)]
        record["sections"]["about"]["teamList"] = [copy.deepcopy(foreign_person)]
        actual = build_tables([record])
        for table in ("workplaces", "contacts", "specialties"):
            self.assertEqual(actual[table], expected[table])
        profile = actual["profiles"][0]
        self.assertEqual(profile["workplace_count"], expected["profiles"][0]["workplace_count"])
        self.assertNotIn("Чужд", profile["full_address"])

    def test_unknown_values_are_blank_and_do_not_create_placeholder_relations(self):
        tables = build_tables([{"profile_id": 123, "category": 101, "status": "listed",
                                "listing": {"basicInfo": {"title": "Д-р Без данни"}},
                                "sections": {}, "errors": []}])
        profile = tables["profiles"][0]
        for field in ("profile_type_id", "profile_city_id", "profile_city", "profile_country_id",
                      "profile_country", "phone", "email", "website", "full_address",
                      "street_address", "address_city", "postal_code", "address_country",
                      "detail_fetched_at"):
            with self.subTest(field=field):
                self.assertEqual(profile[field], "")
        for field in ("workplace_count", "phone_count", "email_count", "address_count"):
            self.assertEqual(profile[field], 0)
        self.assertEqual(tables["workplaces"], [])
        self.assertEqual(tables["contacts"], [])
        self.assertEqual(tables["specialties"], [])
        self.assertNotIn(None, profile.values())

    def test_entity_key_is_stable_and_building_tables_does_not_mutate_source(self):
        record = merge_fixture()
        original = copy.deepcopy(record)
        first = build_tables(iter([record]))
        self.assertEqual(record, original)
        self.assertEqual(first["profiles"][0]["entity_key"], "credoweb:bg:123")
        changed = copy.deepcopy(record)
        changed["sections"]["businessCard"]["title"] = "Ново име"
        changed["url"] = "https://www.credoweb.bg/profile/123/new-slug"
        changed["status"] = "complete"
        second = build_tables([changed])
        self.assertEqual(first["profiles"][0]["entity_key"], second["profiles"][0]["entity_key"])
        self.assertEqual(record, original)


class SnapshotTests(unittest.TestCase):
    def test_read_snapshot_keeps_listed_rows_overlays_details_and_does_not_change_source(self):
        current_listing = {"profileId": 123, "basicInfo": {"title": "Updated listing"}}
        listed_only = {"profileId": 456, "profileType": "page",
                       "basicInfo": {"title": "Listing only", "slug": "listing-only"}}
        processed = merge_fixture()
        processed["listing"] = {"profileId": 123, "basicInfo": {"title": "Old listing"}}
        orphan = {"profile_id": 789, "category": 103, "status": "complete",
                  "sections": {"businessCard": {"title": "Orphan facility"}},
                  "errors": []}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "artificial.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.executescript("""
                    CREATE TABLE listings (profile_id INTEGER PRIMARY KEY, category INTEGER, data TEXT);
                    CREATE TABLE profiles (profile_id INTEGER PRIMARY KEY, category INTEGER, status TEXT, data TEXT);
                    CREATE TABLE responses (url TEXT PRIMARY KEY, data TEXT, fetched_at TEXT);
                """)
                connection.executemany("INSERT INTO listings VALUES (?,?,?)", [
                    (123, 101, json.dumps(current_listing)),
                    (456, 103, json.dumps(listed_only)),
                ])
                connection.executemany("INSERT INTO profiles VALUES (?,?,?,?)", [
                    (123, 101, "partial", json.dumps(processed)),
                    (789, 103, "complete", json.dumps(orphan)),
                ])
                connection.execute("INSERT INTO responses VALUES (?,?,?)",
                                   ("search", "{}", "2026-09-29T12:00:00Z"))
            connection.close()
            original_bytes = database.read_bytes()
            records = read_snapshot(database)
            self.assertIsInstance(records, list)
            by_id = {record["profile_id"]: record for record in records}
            self.assertEqual(len(records), 3)
            self.assertEqual(set(by_id), {123, 456, 789})
            self.assertEqual(by_id[123]["listing"], current_listing)
            self.assertEqual(by_id[123]["sections"], processed["sections"])
            self.assertEqual(by_id[123]["status"], "partial")
            self.assertEqual(by_id[456]["listing"], listed_only)
            self.assertEqual(by_id[456]["sections"], {})
            self.assertEqual(by_id[456]["status"], "listed")
            self.assertEqual(by_id[789], orphan)
            self.assertEqual(database.read_bytes(), original_bytes)
            # Returned records are detached from the source and from later snapshots.
            by_id[123]["listing"]["basicInfo"]["title"] = "Mutated return value"
            reread = {record["profile_id"]: record for record in read_snapshot(database)}
            self.assertEqual(reread[123]["listing"], current_listing)
            self.assertEqual(database.read_bytes(), original_bytes)


if __name__ == "__main__":
    unittest.main()
