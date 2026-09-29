"""Normalized CredoWeb tables for merging with other directories.

Only Python's standard library is required. Source IDs, phone strings and postal
codes remain text. Repeated contacts/specialties/workplaces are child rows, not
pipe-separated cells. The source checkpoint is never modified.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from credoweb_export import plain_text


TABLE_COLUMNS = {
    "profiles": [
        "source", "profile_id", "entity_key", "category_id", "category",
        "profile_type_id", "profile_type", "name", "name_normalized",
        "main_specialty", "profile_city_id", "profile_city", "profile_country_id",
        "profile_country", "phone", "phone_country_code", "email", "website", "full_address",
        "street_address", "address_city", "postal_code", "address_country",
        "workplace_count", "phone_count", "email_count", "address_count",
        "source_url", "record_status", "detail_fetched_at",
    ],
    "workplaces": [
        "profile_id", "workplace_key", "facility_profile_id", "practice_id",
        "facility_name", "position", "street_address", "city_id", "city",
        "postal_code", "country_id", "country", "full_address", "is_current",
        "is_practice", "start_year", "start_month", "end_year", "end_month",
        "source_path",
    ],
    "contacts": [
        "profile_id", "contact_id", "workplace_key", "contact_type",
        "contact_value", "contact_normalized", "phone_country_code", "is_primary", "source_path",
    ],
    "specialties": ["profile_id", "specialty_kind", "specialty_id", "specialty_name"],
}
CATEGORIES = {"101": "Медицински експерти", "103": "Лечебни заведения"}
_WORK_KEYS = {"workplacelist", "workplaces", "practicelist", "practices", "contactslist"}
_CONTACT_KEYS = {
    "phone": {"phone", "phones", "phonelist", "phonenumber", "phonenumbers", "telephone", "mobilephone", "mobile", "fax"},
    "email": {"email", "emails", "emaillist", "emailaddress"},
    "website": {"website", "websites", "websitelist", "webaddress", "site"},
}
_CONTACT_CONTAINERS = {"contact", "contacts", "contactlist", "contactinfo", "about", "basicinfo"}


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return "" if value is None or isinstance(value, (dict, list, bool)) else str(value)


def _id(value: Any) -> str:
    text = _text(value).strip()
    return "" if text in {"", "0", "0.0", "None", "null"} else text


def _normal(value: Any) -> str:
    return " ".join(_text(value).casefold().split())


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _path(parent: str, part: Any) -> str:
    return parent + "/" + str(part).replace("~", "~0").replace("/", "~1")


def _label(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("label", "title", "name", "value", "address"):
            if value.get(key) not in (None, "", [], {}):
                return plain_text(value[key]) if not isinstance(value[key], (dict, list)) else _label(value[key])
        return ""
    return plain_text(_text(value))


def _geo(value: Any) -> tuple[str, str]:
    return (_id(value.get("id")), _label(value)) if isinstance(value, dict) else ("", _label(value))


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return ""


def _flag(value: Any) -> str:
    if value is True or value == 1 or value == "1" or value == "true":
        return "true"
    if value is False or value == 0 or value == "0" or value == "false":
        return "false"
    return ""


def _row(table: str, values: dict) -> dict:
    return {column: values.get(column, "") for column in TABLE_COLUMNS[table]}


def _hash(prefix: str, values: Any) -> str:
    encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return prefix + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _unwrap(value: Any, path: str) -> tuple[dict, str]:
    if isinstance(value, dict) and isinstance(value.get("data"), dict):
        return value["data"], _path(path, "data")
    return _mapping(value), path


def _sources(record: dict) -> list[tuple[str, dict, str]]:
    sections = _mapping(record.get("sections"))
    found = []
    for section_name, value in sections.items():
        kind = _key(section_name.split("?", 1)[0].rsplit("/", 1)[-1])
        if kind not in {"businesscard", "about", "contacts", "tabcontacts"}:
            continue
        data, path = _unwrap(value, _path("/sections", section_name))
        found.append((kind, data, path))
    order = {"businesscard": 0, "about": 1, "contacts": 2, "tabcontacts": 2}
    found.sort(key=lambda source: order[source[0]])
    listing = _mapping(record.get("listing"))
    found.append(("basicinfo", _mapping(listing.get("basicInfo")), "/listing/basicInfo"))
    found.append(("listing", {key: value for key, value in listing.items() if key != "basicInfo"}, "/listing"))
    return found


def _own_value(sources: list, key: str) -> Any:
    return _first(*(source.get(key) for _, source, _ in sources))


def _contact_values(value: Any, path: str, kind: str, phone_country_code: str = "", is_primary: str = ""):
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _contact_values(item, _path(path, index), kind, phone_country_code, is_primary)
    elif isinstance(value, dict):
        if kind == "phone":
            phone_country_code = _text(value.get("phoneCodeStr")).strip() or phone_country_code
            if "primary" in value or "isPrimary" in value:
                is_primary = _flag(value.get("primary", value.get("isPrimary")))
        preferences = {"phone": ("phone", "number", "value", "label", "text"),
                       "email": ("email", "address", "value", "label", "text"),
                       "website": ("website", "url", "value", "label", "text")}[kind]
        for key in preferences:
            if value.get(key) not in (None, "", [], {}):
                yield from _contact_values(value[key], _path(path, key), kind, phone_country_code, is_primary)
                break
        else:
            for key, child in value.items():
                if _key(key) in _CONTACT_KEYS[kind]:
                    yield from _contact_values(child, _path(path, key), kind, phone_country_code, is_primary)
    elif isinstance(value, (str, int)) and not isinstance(value, bool):
        original = str(value)
        if original.strip():
            yield kind, original, path, phone_country_code, is_primary


def _contacts(value: Any, path: str):
    """Inspect contact fields only, never staff/feed/education subtrees."""
    if not isinstance(value, dict):
        return
    for name, child in value.items():
        key = _key(name)
        child_path = _path(path, name)
        contact_type = next((kind for kind, keys in _CONTACT_KEYS.items() if key in keys), None)
        if contact_type:
            yield from _contact_values(child, child_path, contact_type)
        elif key in _CONTACT_CONTAINERS:
            if isinstance(child, list):
                for index, item in enumerate(child):
                    yield from _contacts(item, _path(child_path, index))
            else:
                yield from _contacts(child, child_path)


def normalize_contact(kind: str, value: str) -> str:
    """Conservative matching only; never invent a national/international prefix."""
    trimmed = value.strip()
    if kind == "email":
        return trimmed.lower()
    if kind == "website":
        return trimmed
    if kind == "phone" and re.fullmatch(r"\+?[0-9\s().\-]+", trimmed):
        normalized = re.sub(r"[\s().\-]", "", trimmed)
        return normalized if re.fullmatch(r"\+?[0-9]{1,15}", normalized) else ""
    return ""


def _work_nodes(node: dict, path: str):
    for name, value in node.items():
        if _key(name) in _WORK_KEYS:
            if isinstance(value, list):
                for index, item in enumerate(value):
                    if isinstance(item, dict):
                        yield item, _path(_path(path, name), index)
            elif isinstance(value, dict) and value:
                yield value, _path(path, name)
        elif _key(name) == "about" and isinstance(value, dict):
            yield from _work_nodes(value, _path(path, name))


def _workplace(item: dict, path: str, profile: dict, self_facility: bool) -> dict | None:
    institution = _mapping(item.get("institution"))
    basic = _mapping(item.get("basicInfo"))
    location = _mapping(item.get("location"))
    facility_id = _id(_first(institution.get("id"), institution.get("profileId"), item.get("facilityId"), item.get("profileId")))
    facility_name = _label(_first(institution.get("label"), institution.get("title"), item.get("title"), basic.get("title"), item.get("name")))
    if self_facility and not institution:
        facility_id = profile["profile_id"]
        facility_name = profile["name"]
    city = _first(location.get("city"), item.get("city"), institution.get("city"), basic.get("city"))
    country = _first(location.get("country"), item.get("country"), institution.get("country"), basic.get("country"))
    city_id, city_name = _geo(city)
    country_id, country_name = _geo(country)
    if self_facility:
        if not city_id and not city_name:
            city_id, city_name = profile["profile_city_id"], profile["profile_city"]
        if not country_id and not country_name:
            country_id, country_name = profile["profile_country_id"], profile["profile_country"]
    street = _label(_first(location.get("address"), item.get("streetAddress"), item.get("street_address")))
    published_address = _label(_first(item.get("fullAddress"), item.get("full_address"), item.get("address")))
    # Some practice payloads put a street directly in address. Recognize only
    # obvious street labels; leave explicit/full or ambiguous addresses intact.
    if (not street and published_address and "," not in published_address
            and not item.get("fullAddress") and not item.get("full_address")
            and re.match(r"^(?:ул\.?|бул\.?|пл\.?|кв\.?|ж\.?\s*к\.?|str\.?|street|blvd\.?)\s", published_address, re.IGNORECASE)):
        street, published_address = published_address, ""
    postal = _label(_first(location.get("postCode"), location.get("postcode"), item.get("postCode"), item.get("postcode"), item.get("postal_code")))
    practice_id = _id(_first(item.get("practiceId"), item.get("practice_id")))
    if self_facility and not (street or published_address):
        return None
    if not (facility_id or facility_name or practice_id or street or published_address):
        return None
    address = published_address or (", ".join(part for part in (country_name, city_name, postal, street) if part) if street else "")
    start, end = _mapping(item.get("startDate")), _mapping(item.get("endDate"))
    return _row("workplaces", {
        "profile_id": profile["profile_id"], "facility_profile_id": facility_id,
        "practice_id": practice_id, "facility_name": facility_name,
        "position": _label(_first(item.get("positionTitle"), item.get("position"), item.get("jobTitle"))),
        "street_address": street, "city_id": city_id, "city": city_name,
        "postal_code": postal, "country_id": country_id, "country": country_name,
        "full_address": address, "is_current": _flag(item.get("currentWork")),
        "is_practice": _flag(item.get("practice")), "start_year": _text(start.get("year")),
        "start_month": _text(start.get("month")), "end_year": _text(end.get("year")),
        "end_month": _text(end.get("month")), "source_path": path,
    })


def _compatible(left: dict, right: dict) -> bool:
    left_id, right_id = left["facility_profile_id"], right["facility_profile_id"]
    if left_id and right_id:
        if left_id != right_id:
            return False
    elif left["facility_name"] and right["facility_name"]:
        if _normal(left["facility_name"]) != _normal(right["facility_name"]):
            return False
    elif not any((left_id, right_id, left["facility_name"], right["facility_name"])):
        if not (left["street_address"] or left["full_address"]):
            return False
    for key in ("practice_id", "street_address", "city_id", "city", "postal_code", "country_id", "country", "position", "is_current", "is_practice", "start_year", "start_month", "end_year", "end_month"):
        if left[key] and right[key] and _normal(left[key]) != _normal(right[key]):
            return False
    if not left["street_address"] and not right["street_address"] and left["full_address"] and right["full_address"]:
        if _normal(left["full_address"]) != _normal(right["full_address"]):
            return False
    # At least one shared identity/location anchor is required.
    return bool((left_id and left_id == right_id)
                or (left["facility_name"] and _normal(left["facility_name"]) == _normal(right["facility_name"]))
                or (left["street_address"] and _normal(left["street_address"]) == _normal(right["street_address"]))
                or (left["full_address"] and _normal(left["full_address"]) == _normal(right["full_address"])))


def _work_key(row: dict) -> str:
    return _hash("cw-work-", [row["profile_id"], row["facility_profile_id"] or _normal(row["facility_name"]),
                             row["practice_id"], row["city_id"], row["country_id"], *(_normal(row[key]) for key in ("street_address", "city", "postal_code", "country")),
                             _normal(row["full_address"]) if not row["street_address"] else "",
                             _normal(row["position"]), row["is_current"], row["is_practice"], row["start_year"], row["start_month"], row["end_year"], row["end_month"]])


def _profile(record: dict, sources: list) -> dict:
    pid = _id(record.get("profile_id")) or _id(_mapping(record.get("listing")).get("profileId"))
    category_id = _id(record.get("category"))
    profile_type = _own_value(sources, "profileType")
    type_obj = _mapping(profile_type)
    category_id = category_id or _id(type_obj.get("category"))
    name = _label(_own_value(sources, "title"))
    if not name:
        name = _label(_first(*(_mapping(source.get("about")).get("title") for _, source, _ in sources)))
    city_id, city = _geo(_own_value(sources, "city"))
    country_id, country = _geo(_own_value(sources, "country"))
    source_url = _text(_first(_own_value(sources, "canonicalLink"), record.get("url")))
    if not source_url and pid:
        slug = _text(_own_value(sources, "slug"))
        prefix = "page" if category_id == "103" else "profile"
        source_url = f"https://www.credoweb.bg/{prefix}/{pid}/{slug}"
    status = _text(record.get("status"))
    return _row("profiles", {
        "source": "credoweb", "profile_id": pid, "entity_key": "credoweb:bg:" + pid,
        "category_id": category_id, "category": CATEGORIES.get(category_id, category_id),
        "profile_type_id": _id(type_obj.get("id")), "profile_type": _label(profile_type),
        "name": name, "name_normalized": _normal(name), "profile_city_id": city_id,
        "profile_city": city, "profile_country_id": country_id, "profile_country": country,
        "source_url": source_url, "record_status": status,
        "detail_fetched_at": "" if status in {"listed", "pending"} else _text(record.get("fetched_at")),
    })


def _specialties(profile_id: str, sources: list) -> list[dict]:
    result, seen = [], set()
    for _, node, _ in sources:
        specialties = _mapping(node.get("specialityList"))
        for kind, values in (("main", _first(node.get("mainSpeciality"), node.get("mainSpecialityList"), specialties.get("main"))),
                             ("other", _first(node.get("otherSpeciality"), node.get("otherSpecialityList"), specialties.get("other")))):
            for item in values if isinstance(values, list) else ([values] if values else []):
                sid = _id(item.get("id")) if isinstance(item, dict) else ""
                name = _label(item)
                identity = kind, sid or _normal(name)
                if (sid or name) and identity not in seen:
                    seen.add(identity)
                    result.append(_row("specialties", {"profile_id": profile_id, "specialty_kind": kind, "specialty_id": sid, "specialty_name": name}))
    return result


def _build_one(record: dict) -> tuple[dict, list[dict], list[dict], list[dict]]:
    sources = _sources(record)
    profile = _profile(record, sources)
    pid = profile["profile_id"]
    facility = profile["category_id"] == "103"
    candidates, own_contacts = [], []
    source_order = 0
    for kind, node, path in sources:
        self_row = _workplace(node, path, profile, True) if facility and kind in {"businesscard", "about", "basicinfo"} else None
        if self_row:
            candidates.append({"row": self_row, "contacts": list(_contacts(node, path)), "order": source_order})
            source_order += 1
        else:
            own_contacts.extend(_contacts(node, path))
        for item, item_path in _work_nodes(node, path):
            self_facility = facility and not _mapping(item.get("institution"))
            row = _workplace(item, item_path, profile, self_facility)
            contacts = list(_contacts(item, item_path))
            if row:
                candidates.append({"row": row, "contacts": contacts, "order": source_order})
                source_order += 1
            else:
                own_contacts.extend(contacts)
    groups = []
    candidates.sort(key=lambda item: (-sum(value != "" for value in item["row"].values()), item["order"]))
    for candidate in candidates:
        matches = [group for group in groups if _compatible(group["row"], candidate["row"])]
        if len(matches) > 1:
            exact = [group for group in matches if _work_key(group["row"]) == _work_key(candidate["row"])]
            if len(exact) == 1:
                matches = exact
        if len(matches) == 1:
            group = matches[0]
            for key, value in candidate["row"].items():
                if group["row"][key] == "" and value != "":
                    group["row"][key] = value
            group["contacts"].extend(candidate["contacts"])
            group["order"] = min(group["order"], candidate["order"])
        else:
            groups.append(candidate)
    groups.sort(key=lambda item: item["order"])
    contacts, contact_groups = [], {}

    def append_contacts(items, workplace_key=""):
        for contact_type, original, path, country_code, primary in items:
            identity = workplace_key, contact_type, original
            existing = contact_groups.setdefault(identity, [])
            exact = [row for row in existing if row["phone_country_code"] == country_code]
            target = exact[0] if exact else None
            if target is None and len(existing) == 1 and (not country_code or not existing[0]["phone_country_code"]):
                target = existing[0]
            if target is not None:
                if not target["phone_country_code"] and country_code:
                    target["phone_country_code"] = country_code
                    target["source_path"] = path
                if not target["is_primary"] and primary:
                    target["is_primary"] = primary
                continue
            row = _row("contacts", {"profile_id": pid,
                "workplace_key": workplace_key, "contact_type": contact_type,
                "contact_value": original, "contact_normalized": normalize_contact(contact_type, original),
                "phone_country_code": country_code, "is_primary": primary, "source_path": path})
            contacts.append(row)
            existing.append(row)

    append_contacts(own_contacts)
    workplaces = []
    for group in groups:
        row = group["row"]
        row["workplace_key"] = _work_key(row)
        workplaces.append(row)
        append_contacts(group["contacts"], row["workplace_key"])
    for row in contacts:
        row["contact_id"] = _hash("cw-contact-", [pid, row["workplace_key"], row["contact_type"], row["contact_value"], row["phone_country_code"]])
    specialities = _specialties(pid, sources)
    profile["main_specialty"] = next((row["specialty_name"] for row in specialities if row["specialty_kind"] == "main"), "")
    for kind in ("phone", "email", "website"):
        values = list(dict.fromkeys(row["contact_value"] for row in contacts if row["contact_type"] == kind))
        profile[kind] = values[0] if values else ""
        if kind == "phone":
            profile["phone_country_code"] = next((row["phone_country_code"] for row in contacts if row["contact_type"] == "phone"), "")
        if kind in {"phone", "email"}:
            profile[kind + "_count"] = len(values)
    addresses = [row for row in workplaces if row["full_address"] or row["street_address"]]
    if addresses:
        first = addresses[0]
        for target, source in (("full_address", "full_address"), ("street_address", "street_address"), ("address_city", "city"), ("postal_code", "postal_code"), ("address_country", "country")):
            profile[target] = first[source]
    profile["workplace_count"] = len(workplaces)
    profile["address_count"] = len({tuple(_normal(row[key]) for key in ("street_address", "city", "postal_code", "country", "full_address")) for row in addresses})
    return profile, workplaces, contacts, specialities


def build_tables(records: Iterable[dict]) -> dict[str, list[dict]]:
    """Transform raw records. Primary contacts prefer profile-level then workplaces.

    Primary address is the first available workplace/address in source order.
    Contact counts are distinct original published values across all scopes.
    Name normalization only casefolds and collapses whitespace. No fuzzy person
    matching or guessed country prefixes are applied. Explicit conflicting roles
    or employment dates at one facility remain separate workplace rows.
    """
    tables = {name: [] for name in TABLE_COLUMNS}
    by_id = {}
    for record in records:
        pid = _id(record.get("profile_id")) or _id(_mapping(record.get("listing")).get("profileId"))
        if not pid:
            continue
        # A duplicate profile ID represents the same source entity. Prefer the
        # record with more fetched sections; ties retain the first occurrence.
        if pid not in by_id or len(_mapping(record.get("sections"))) > len(_mapping(by_id[pid].get("sections"))):
            by_id[pid] = record
    for record in by_id.values():
        profile, workplaces, contacts, specialties = _build_one(record)
        tables["profiles"].append(profile)
        tables["workplaces"].extend(workplaces)
        tables["contacts"].extend(contacts)
        tables["specialties"].extend(specialties)
    return tables


def read_snapshot(database_path: Path | str) -> list[dict]:
    """Copy SQLite to memory read-only, close source, then materialize records.

    The online backup copies 256 pages per step so transformation never holds a
    source cursor/read transaction while the collector is trying to commit.
    """
    database_path = Path(database_path).resolve()
    memory = sqlite3.connect(":memory:")
    try:
        source = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            source.backup(memory, pages=256, sleep=0.01)
        finally:
            source.close()
        table_names = {row[0] for row in memory.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"listings", "profiles"}.issubset(table_names):
            raise ValueError("Checkpoint must contain listings and profiles tables")
        fetched_at = ""
        if "responses" in table_names:
            fetched_at = memory.execute("SELECT MAX(fetched_at) FROM responses").fetchone()[0] or ""
        records = []
        for pid, category, listing_data, profile_data in memory.execute("""
                SELECT l.profile_id,l.category,l.data,p.data FROM listings l
                LEFT JOIN profiles p ON p.profile_id=l.profile_id ORDER BY l.category,l.profile_id"""):
            listing = json.loads(listing_data)
            if profile_data:
                record = json.loads(profile_data)
                record.update({"profile_id": pid, "category": category, "listing": listing})
            else:
                basic = _mapping(listing.get("basicInfo"))
                prefix = "page" if listing.get("profileType") == "page" else "profile"
                record = {"profile_id": pid, "category": category, "listing": listing,
                          "url": f"https://www.credoweb.bg/{prefix}/{pid}/{basic.get('slug', '')}",
                          "sections": {}, "status": "listed", "errors": [], "fetched_at": fetched_at}
            records.append(record)
        for pid, category, data in memory.execute("""SELECT p.profile_id,p.category,p.data FROM profiles p
                LEFT JOIN listings l ON l.profile_id=p.profile_id WHERE l.profile_id IS NULL
                ORDER BY p.category,p.profile_id"""):
            record = json.loads(data)
            record.update({"profile_id": pid, "category": category})
            records.append(record)
        return records
    finally:
        memory.close()


def _snapshot_metadata(tables: dict, database: Path, snapshot_at: str) -> dict:
    statuses = {}
    for row in tables["profiles"]:
        status = row["record_status"]
        statuses[status] = statuses.get(status, 0) + 1
    return {"schema_version": "1.0", "snapshot_at": snapshot_at, "database": database.name,
            "counts": {name: len(rows) for name, rows in tables.items()},
            "record_status_counts": statuses, "partial_snapshot": any(status != "complete" and count for status, count in statuses.items()),
            "encoding": "UTF-8 BOM", "delimiter": ","}


def _schema_document() -> dict:
    primary_keys = {
        "profiles": ["profile_id"], "workplaces": ["workplace_key"],
        "contacts": ["contact_id"],
        "specialties": ["profile_id", "specialty_kind", "specialty_id", "specialty_name"],
    }
    schema = {"schema_version": "1.0", "null_value": "", "encoding": "UTF-8 BOM", "delimiter": ",", "tables": {}}
    for name, columns in TABLE_COLUMNS.items():
        fields = {column: {"type": "integer" if column.endswith("_count") else "text"} for column in columns}
        for column in ("is_current", "is_practice", "is_primary"):
            if column in fields:
                fields[column]["allowed_values"] = ["true", "false", ""]
        if "contact_type" in fields:
            fields["contact_type"]["allowed_values"] = ["phone", "email", "website"]
        if "specialty_kind" in fields:
            fields["specialty_kind"]["allowed_values"] = ["main", "other"]
        entry = {"columns": list(columns), "primary_key": primary_keys[name], "fields": fields}
        if name != "profiles":
            entry["foreign_keys"] = [{"columns": ["profile_id"], "references": "profiles.profile_id"}]
        if name == "contacts":
            entry["foreign_keys"].append({"columns": ["workplace_key"], "references": "workplaces.workplace_key", "nullable": True})
        schema["tables"][name] = entry
    return schema


_MERGE_README = """# CredoWeb CSV tables

Start with **profiles.csv**: one row per CredoWeb profile. The scraper refreshes
these CSV files periodically in place while it collects data, and once more when
the run finishes or stops. The file names stay the same; no archive is required.
Use **manifest.json** for the published snapshot time, counts and collection
status. A successful CSV refresh does not mean that collection has finished.

## Files and relationships

| File | One row represents | Join |
| --- | --- | --- |
| profiles.csv | One profile with its first available contact and address | Unique profile_id; cross-source key entity_key |
| workplaces.csv | One distinct workplace or facility location | profile_id |
| contacts.csv | One published phone, email or website in its profile/workplace context | profile_id; optional workplace_key |
| specialties.csv | One main or other specialty | profile_id |

**schema.json** describes the columns, types and table keys. Source labels remain
in their original language; column names use English snake_case.

## Import settings

- Format: standard comma-delimited CSV, quoted when needed, UTF-8 with BOM and
  CRLF row endings. CSV does not store column types.
- Import IDs, phone numbers and postcodes as **Text**. In Excel use
  **Data -> From Text/CSV**, select comma and disable automatic numeric conversion
  for these columns. Treat source text as text, not spreadsheet formulas.
- Phone strings retain leading zeros and plus signs. No Excel-specific apostrophe
  prefixes are added to the data. Original values remain in contact_value.
- An empty field means unknown/unavailable. It is not zero or proof that a contact
  does not exist. Boolean fields use true, false or an empty value for unknown.

## Merge rules

1. Match repeated CredoWeb snapshots on profile_id or entity_key
   (credoweb:bg:<profile_id>). Do not identify people by name alone.
2. Join each child table on profile_id. A facility_profile_id may identify a
   facility outside the collected directory, so use a left join for that link.
3. workplace_key and contact_id are deterministic from the available data and
   can change when a location or contact is enriched. Replace the child rows for
   each affected profile when loading a newer snapshot.
4. name_normalized only casefolds and collapses whitespace. contact_normalized
   is a matching aid, not a unique person identifier. Shared numbers and emails
   need review before merging people across different sources.
5. profile_city describes the profile. Address fields and workplaces describe a
   practice/facility location, which can be in a different city.

The primary phone, email, website, specialty and address columns show the first
available value, not a recommended contact or principal practice. Additional
values remain in the child tables. Profile phone/email counts represent distinct
original values, so contact-row counts can differ across workplaces.

Phone normalization removes formatting only from unambiguous single numbers
with at most 15 digits. It does not guess country codes or split ambiguous
strings. Explicit source codes remain in phone_country_code and an explicit
source preference flag remains in is_primary. The normalized number does not
automatically prepend the separate code. Emails are trimmed and lowercased;
websites are trimmed. Conflicting known workplace roles, dates or flags remain
separate relationships.

## Progress and provenance

record_status retains collection progress: listed has directory data,
in_progress has some fetched sections, complete has all advertised sections
processed, and partial has missing/failed sections. Blank contact/address fields
can still be awaiting collection. partial_snapshot describes record completeness;
consult the collection manifest for directory discovery coverage.

source_url links to each profile. source_path identifies the source field for a
workplace or contact. detail_fetched_at is a saved profile timestamp, not a new
verification date for cached data. The tables focus on identity, contacts,
locations and specialties. Full descriptions and other raw fields remain in the
scraper's full exports and saved source records.

Published GitHub data snapshots also contain full/profiles.csv.gz,
full/workplaces.csv.gz and full/manifest.json. Those are the detailed Bulgarian
semicolon-delimited exports, including workplace JSON and observed coordinates.
The detailed manifest links to the exact normalized manifest checksum. Read all
files from one commit and validate both compressed and uncompressed checksums.
The detailed publication contract is documented in docs/full-publication.md on
the source repository's default branch.

## Reading during an update

Files are staged first and each file is replaced atomically. **manifest.json is
published last**. Its files entries include CSV row counts and SHA-256 hashes;
support_files contains the schema and README hashes. Readers that need a
consistent multi-file snapshot should read the manifest, load and verify the
files, and check that the manifest has not changed. Retry if a hash differs or
the manifest changes. A locked file can delay publication; the scraper retries.

## Optional standalone refresh

The scraper handles regular updates. To regenerate these files from a saved
checkpoint without fetching websites, run from the scraper folder:

```powershell
python .\\credoweb_merge.py --database output/checkpoint.sqlite3 --output output/merge
```

The standalone command reads a consistent in-memory SQLite snapshot and closes
the source connection before transforming data. It does not stop the collector.
"""


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def export_merge_records(records: Iterable[dict], output_dir: Path | str, *,
                         database: Path | str = "checkpoint.sqlite3",
                         snapshot_at: str | None = None,
                         collection_status: str | None = None) -> dict:
    """Publish the reusable CSV tables and their schema, docs and manifest.

    All serialization finishes in owned temporary files before any destination
    is replaced. Individual files are atomic; the manifest is the last commit
    marker and contains hashes so readers can detect an update in progress.
    OSError (including a destination locked by Excel) propagates for the caller
    to retry. Existing archives and unrelated files are never touched.
    """
    tables = build_tables(records)
    snapshot_at = snapshot_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata = _snapshot_metadata(tables, Path(database), snapshot_at)
    metadata.update({"source": "CredoWeb public directory", "publication_status": "complete",
                     "collection_status": collection_status or "unknown", "files": {}, "support_files": {}})
    staged: dict[str, Path] = {}

    def temporary(name: str) -> Path:
        descriptor, location = tempfile.mkstemp(prefix=".credoweb-merge-", suffix=".tmp", dir=output_dir)
        os.close(descriptor)
        staged[name] = Path(location)
        return staged[name]

    def stage_text(name: str, text: str) -> None:
        with temporary(name).open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    try:
        for name, rows in tables.items():
            filename = name + ".csv"
            path = temporary(filename)
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=TABLE_COLUMNS[name], delimiter=",", lineterminator="\r\n")
                writer.writeheader()
                writer.writerows(rows)
            metadata["files"][name] = {"filename": filename, "rows": len(rows),
                                        "columns": len(TABLE_COLUMNS[name]), "sha256": _file_hash(path),
                                        "bytes": path.stat().st_size}
        stage_text("schema.json", json.dumps(_schema_document(), ensure_ascii=False, indent=2) + "\n")
        stage_text("README.md", _MERGE_README)
        for filename in ("schema.json", "README.md"):
            metadata["support_files"][filename] = {"filename": filename, "sha256": _file_hash(staged[filename]), "bytes": staged[filename].stat().st_size}
        stage_text("manifest.json", json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
        for filename, temporary_path in staged.items():
            if filename != "manifest.json":
                os.replace(temporary_path, output_dir / filename)
        os.replace(staged["manifest.json"], output_dir / "manifest.json")
    finally:
        for temporary_path in staged.values():
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    return {"counts": metadata["counts"],
            "files": {filename: str(output_dir / filename) for filename in staged},
            "snapshot_at": snapshot_at}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("output/checkpoint.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("output/merge"))
    parser.add_argument("--json", type=Path, help="Write JSON intermediate only, without CSV files")
    args = parser.parse_args(argv)
    records = read_snapshot(args.database)
    snapshot_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if args.json:
        tables = build_tables(records)
        metadata = _snapshot_metadata(tables, args.database, snapshot_at)
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"tables": tables, "columns": TABLE_COLUMNS, **metadata}, ensure_ascii=False), encoding="utf-8")
    else:
        metadata = export_merge_records(records, args.output, database=args.database, snapshot_at=snapshot_at)
    print(json.dumps(metadata, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
