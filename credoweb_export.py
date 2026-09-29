"""Portable CSV and offline HTML exports for public CredoWeb profile records.

``export_records(records, output_dir)`` accepts an iterable (including a generator).
It returns row counts and output paths. CSV files use UTF-8 BOM and a semicolon
delimiter for Bulgarian Excel. ``details.csv`` is the complete JSON tree: every
node has an RFC 6901 JSON Pointer, a type and a JSON value. Object/array values
are empty container markers; their children have separate rows. The JSON value
column preserves strings, numbers, nulls, empty containers and array ordering.

No network calls or non-standard Python dependencies are used here.
"""

from __future__ import annotations

import csv
import json
import os
import re
import tempfile
from contextlib import ExitStack
from datetime import datetime, timezone
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Iterator


PROFILE_COLUMNS = [
    "ID", "Категория", "Тип профил", "Име", "Основни специалности",
    "Други специалности", "Град", "Държава", "Адреси", "Телефони",
    "Имейли", "Уебсайтове", "Длъжност", "Описание", "Услуги",
    "Здравно осигуряване", "Работно време", "Снимка", "Източник",
    "Извлечено на", "Статус", "Брой грешки", "Налични секции",
]
WORKPLACE_COLUMNS = [
    "ID профил", "Име профил", "Вид запис", "ID място", "Наименование",
    "Длъжност", "Град", "Държава", "Адреси", "Телефони", "Имейли",
    "Уебсайтове", "Работно време", "Здравно осигуряване", "Текуща месторабота",
    "Практика", "От", "До", "Описание", "С предварително записване",
    "Платен прием", "Прием по осигуряване", "Пощенски код", "Източник поле",
    "Източник профил", "JSON запис",
]
DETAIL_COLUMNS = [
    "ID профил", "Име профил", "Секция", "Път JSON Pointer", "Тип",
    "Стойност", "JSON стойност", "Източник", "Извлечено на",
]
ERROR_COLUMNS = [
    "ID профил", "Име профил", "Секция", "Грешка", "JSON грешка",
    "Източник", "Извлечено на",
]


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: Any) -> str:
    """Make a readable summary; the complete original remains in details.csv."""
    if value is None:
        return ""
    text = str(value)
    if "<" in text or "&" in text:
        parser = _PlainText()
        parser.feed(text)
        text = "".join(parser.parts)
    return "\n".join(
        line.strip() for line in re.sub(r"[^\S\n]+", " ", text).splitlines()
        if line.strip()
    )


def safe_csv(value: Any) -> Any:
    """Prevent downloaded text from being interpreted as an Excel formula."""
    if not isinstance(value, str):
        return value
    probe = value.lstrip(" \t\r\n\v\f\ufeff\x00")
    if (probe.startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n"))
            or re.match(r"^0\d", probe) or re.fullmatch(r"\d{16,}", probe)):
        return "'" + value
    return value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@lru_cache(maxsize=2048)
def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict) and "data" in value and isinstance(value["data"], (dict, list)):
        return value["data"]
    return value


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _labels(value: Any) -> list[str]:
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, list):
        return [label for item in value for label in _labels(item)]
    if isinstance(value, dict):
        for name in ("value", "number", "phone", "email", "website", "url", "address", "label", "title", "name", "text"):
            if name in value and value[name] not in (None, "", [], {}):
                return _labels(value[name])
        return [label for name, item in value.items()
                if _key(name) not in {"id", "privacy", "typeid", "profileid", "order", "verified"}
                for label in _labels(item)]
    return [plain_text(value)] if str(value).strip() else []


def _join(values: Iterable[Any]) -> str:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for label in _labels(value):
            if label and label not in seen:
                seen.add(label)
                result.append(label)
    return " | ".join(result)


def _find(value: Any, names: set[str]) -> Iterator[Any]:
    if isinstance(value, dict):
        for name, child in value.items():
            if _key(name) in names:
                yield child
            else:
                yield from _find(child, names)
    elif isinstance(value, list):
        for child in value:
            yield from _find(child, names)


def _gather(sources: Iterable[Any], *names: str) -> str:
    keys = {_key(name) for name in names}
    return _join(value for source in sources for value in _find(source, keys))


def _structured(sources: Iterable[Any], *names: str) -> str:
    """Keep grouping for schedules and dates instead of mixing their labels."""
    keys = {_key(name) for name in names}
    values = (_json(value) if isinstance(value, (dict, list)) else str(value)
              for source in sources for value in _find(source, keys)
              if value not in (None, "", [], {}))
    return " | ".join(dict.fromkeys(values))


def _yes_no(value: Any) -> str:
    return "Да" if value is True else "Не" if value is False else ""


def _section(record: dict, *names: str) -> dict:
    names_normalized = {_key(name) for name in names}
    for name, data in _mapping(record.get("sections")).items():
        normalized = _key(name.split("?", 1)[0])
        if normalized in names_normalized or any(normalized.endswith(n) for n in names_normalized):
            return _mapping(_unwrap(data))
    return {}


def _identity_sources(record: dict) -> list[dict]:
    listing = _mapping(record.get("listing"))
    return [
        _section(record, "businessCard"),
        _mapping(listing.get("basicInfo")),
        _section(record, "about"),
        _section(record, "contacts", "tabContacts"),
        {key: value for key, value in listing.items() if key != "basicInfo"},
    ]


def _first(sources: Iterable[dict], name: str) -> Any:
    for source in sources:
        value = source.get(name)
        if value not in (None, "", [], {}):
            return value
    return ""


def _address_key(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold()))


def _address_candidates(value: Any) -> Iterator[str]:
    """Combine fields belonging to one location, never across practice entries."""
    if isinstance(value, list):
        for item in value:
            yield from _address_candidates(item)
    elif isinstance(value, dict):
        location = _mapping(value.get("location"))
        # The API supplies both a formatted address and a location.address street.
        # Prefer its formatted address; fill only components absent from it.
        street = _join([value.get("address") or location.get("address")])
        if street:
            sources = [location, value, _mapping(value.get("basicInfo")),
                       _mapping(value.get("institution"))]
            components = [
                _join([_first(sources, "country")]),
                _join([_first(sources, "city")]),
                _join([_first(sources, "postCode") or _first(sources, "postcode")]),
            ]
            parts = {_address_key(part) for part in re.split(r"[,;\n]", street)}
            prefix = []
            for component in components:
                key = _address_key(component)
                if key and key not in parts:
                    prefix.append(component)
                    parts.add(key)
            yield ", ".join([*prefix, street])
        for name, child in value.items():
            if name == "address" or (street and name == "location"):
                continue
            yield from _address_candidates(child)


def _full_addresses(sources: Iterable[Any]) -> str:
    addresses: list[tuple[str, str]] = []
    for source in sources:
        for address in _address_candidates(source):
            key = _address_key(address)
            if not key or any(existing == key or existing.endswith(" " + key)
                              for existing, _ in addresses):
                continue
            # About/contact sections may repeat the same address without country.
            addresses = [(existing, text) for existing, text in addresses
                         if not key.endswith(" " + existing)]
            addresses.append((key, address))
    return "\n".join(text for _, text in addresses)


def profile_row(record: dict) -> dict:
    """Curate identity/contact fields without mixing in staff or article authors."""
    sources = _identity_sources(record)
    category = record.get("category", "")
    category_name = {"101": "Медицински експерти", "103": "Лечебни заведения"}.get(str(category), str(category))
    main = [_first(sources, "mainSpeciality"), _first(sources, "mainSpecialityList")]
    other = [_first(sources, "otherSpeciality"), _first(sources, "otherSpecialityList")]
    for source in sources:
        specialities = _mapping(source.get("specialityList"))
        main.append(specialities.get("main"))
        other.append(specialities.get("other"))
    description = _gather(sources, "description", "biography", "aboutMe", "presentation")
    photo = _join([_first(sources, "photo")])
    if photo.startswith("//"):
        photo = "https:" + photo
    return dict(zip(PROFILE_COLUMNS, [
        record.get("profile_id", ""), category_name,
        _join([_first(sources, "profileType")]), _join([_first(sources, "title")]),
        _join(main), _join(other), _join([_first(sources, "city")]),
        _join([_first(sources, "country")]), _full_addresses(sources),
        _gather(sources, "phoneList", "phone", "phones", "phoneNumbers", "telephone", "mobilePhone", "fax"),
        _gather(sources, "emailList", "email"),
        _gather(sources, "websiteList", "website", "websites"),
        _gather(sources, "position", "positionTitle", "otherPosition", "jobTitle"), description,
        _gather(sources, "serviceList", "services"),
        _gather(sources, "insurer", "insurerList", "insurers"),
        _structured(sources, "workingDays", "workingHours", "schedule", "consultationHours"),
        photo, record.get("url", ""), record.get("fetched_at", ""),
        record.get("status", ""), len(record.get("errors") or []),
        " | ".join(_mapping(record.get("sections")).keys()),
    ]))


def _pointer(parent: str, name: Any) -> str:
    return parent + "/" + str(name).replace("~", "~0").replace("/", "~1")


def flatten_json(value: Any, path: str = "") -> Iterator[tuple[str, str, Any, str]]:
    """Yield every node, including containers, so the JSON tree is recoverable."""
    if isinstance(value, dict):
        yield path, "object", "", "{}"
        for name, child in value.items():
            yield from flatten_json(child, _pointer(path, name))
    elif isinstance(value, list):
        yield path, "array", "", "[]"
        for index, child in enumerate(value):
            yield from flatten_json(child, _pointer(path, index))
    elif value is None:
        yield path, "null", "", "null"
    elif isinstance(value, bool):
        yield path, "boolean", str(value).lower(), _json(value)
    elif isinstance(value, (int, float)):
        yield path, "number", value, _json(value)
    else:
        yield path, "string", str(value), _json(value)


def _work_items(value: Any, path: str = "") -> Iterator[tuple[str, str, dict]]:
    """Keep each occurrence/address with its source path; do not merge workplaces."""
    if isinstance(value, dict):
        for name, child in value.items():
            child_path = _pointer(path, name)
            normalized = _key(name)
            if normalized in {"workplacelist", "practicelist", "contactslist", "workplaces", "practices"}:
                kind = {"workplacelist": "Месторабота", "workplaces": "Месторабота",
                        "practicelist": "Практика", "practices": "Практика",
                        "contactslist": "Контакти и адрес"}[normalized]
                if isinstance(child, list):
                    for index, item in enumerate(child):
                        if isinstance(item, dict):
                            yield kind, _pointer(child_path, index), item
                elif isinstance(child, dict) and child:
                    yield kind, child_path, child
            else:
                yield from _work_items(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _work_items(child, _pointer(path, index))


def workplace_rows(record: dict, summary: dict) -> Iterator[dict]:
    items = list(_work_items({"listing": record.get("listing", {}), "sections": record.get("sections", {})}))
    card = _section(record, "businessCard")
    card_address = _full_addresses([card]) if str(record.get("category")) == "103" else ""
    if card_address and not any(_full_addresses([item]) == card_address for _, _, item in items):
        # The priority address pass can precede the facility's contacts section.
        items.insert(0, ("Адрес на лечебно заведение", "/sections/businessCard", card))
    for kind, path, item in items:
        basic = _mapping(item.get("basicInfo"))
        institution = _mapping(item.get("institution"))
        sources = [item, basic]
        yield dict(zip(WORKPLACE_COLUMNS, [
            record.get("profile_id", ""), summary["Име"], kind,
            item.get("profileId", item.get("practiceId", institution.get("id", item.get("id", "")))),
            _join([_first(sources, "title") or _first(sources, "name") or institution.get("label", "")]),
            _gather(sources, "position", "positionTitle", "jobTitle"), _gather(sources, "city"),
            _gather(sources, "country"), _full_addresses([item]),
            _gather(sources, "phoneList", "phone", "phones", "phoneNumbers", "telephone", "mobilePhone", "fax"),
            _gather(sources, "emailList", "email"),
            _gather(sources, "websiteList", "website", "websites"),
            _structured(sources, "workingDays", "workingHours", "schedule", "consultationHours"),
            _gather(sources, "insurer", "insurerList", "insurers"),
            _yes_no(item.get("currentWork")), _yes_no(item.get("practice")),
            _structured(sources, "startDate"), _structured(sources, "endDate"),
            _gather(sources, "description"), _yes_no(item.get("appointmentNeeded")),
            _yes_no(item.get("paidAcceptance")), _yes_no(item.get("insuranceAcceptance")),
            _gather(sources, "postCode", "postcode"), path, record.get("url", ""),
            _json(item),
        ]))


def _html_json(value: Any) -> str:
    return (_json(value).replace("&", "\\u0026").replace("<", "\\u003c")
            .replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _run_metadata(output_dir: Path) -> dict:
    """Read optional crawl coverage without coupling the exporter to the crawler."""
    try:
        manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    discovery = _mapping(manifest.get("discovery"))
    status = manifest.get("status", "")
    if not isinstance(status, str) or status not in {"complete", "partial", "sample", "interrupted", "failed", "running"}:
        status = ""
    running = status == "running"
    if status not in {"interrupted", "failed"} and (discovery.get("limited") or any(_mapping(manifest.get("limits")).values())):
        status = "sample"
    def number(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    categories = []
    source_categories = discovery.get("categories")
    for item in source_categories if isinstance(source_categories, list) else []:
        if isinstance(item, dict) and number(item.get("category")) is not None:
            categories.append({"category": item["category"], "expected": number(item.get("expected")), "discovered": number(item.get("discovered"))})
    expected = number(discovery.get("expected"))
    if expected is None and categories and all(item["expected"] is not None for item in categories):
        expected = sum(item["expected"] for item in categories)
    stage = manifest.get("stage", "")
    if stage not in ("discover", "enrich", "completed"):
        stage = ""
    merge = _mapping(manifest.get("merge_exports"))
    return {"status": status, "expected": expected, "stage": stage, "running": running, "categories": categories,
            "merge_snapshot_at": str(merge.get("snapshot_at") or ""),
            "merge_export_failed": manifest.get("merge_export_status") == "failed"}


def _merge_links(output_dir: Path) -> str:
    names = {"profiles": "Профили", "workplaces": "Местоработи и адреси",
             "contacts": "Контакти", "specialties": "Специалности"}
    if not all((output_dir / "merge" / f"{name}.csv").is_file() for name in names):
        return ""
    links = ' · '.join(f'<a href="merge/{name}.csv" download>{label}</a>' for name, label in names.items())
    return '<div class="merge-downloads"><strong>Структурирани CSV за обединяване:</strong> ' + links + '<div id="merge-updated" class="small muted"></div></div>'


def export_records(records: Iterable[dict], output_dir: Path | str, *, include_details: bool = True) -> dict:
    """Write four CSVs and report.html; return {counts: {...}, files: {...}}.

    The HTML contains all curated profile/workplace rows, with client-side
    filtering and pagination. Unknown API fields remain in details.csv and the
    crawler's raw JSON files. Files are replaced only after successful export.
    For a quick progressive snapshot, include_details=False writes only profiles,
    workplaces and HTML, preserving any older details/errors files and omitting
    their links from the new report. The returned details count is then None.
    """
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_metadata = _run_metadata(output_dir)
    run_metadata.update({"details_current": include_details, "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    filenames = ("profiles.csv", "workplaces.csv", "details.csv", "errors.csv", "report.html") if include_details else ("profiles.csv", "workplaces.csv", "report.html")
    temporary: dict[str, Path] = {}
    counts = {"profiles": 0, "workplaces": 0, "details": 0 if include_details else None, "errors": 0}
    category_counts: dict[str, dict] = {}
    profile_statuses: dict[str, int] = {}
    try:
        with ExitStack() as stack:
            handles = {}
            for name in filenames:
                descriptor, temp_path = tempfile.mkstemp(prefix=".credoweb-", suffix=".tmp", dir=output_dir)
                os.close(descriptor)
                temporary[name] = Path(temp_path)
                handles[name] = stack.enter_context(open(temp_path, "w", encoding="utf-8" if name.endswith(".html") else "utf-8-sig", newline=""))
            writers = {}
            for name, columns in (("profiles.csv", PROFILE_COLUMNS), ("workplaces.csv", WORKPLACE_COLUMNS), ("details.csv", DETAIL_COLUMNS), ("errors.csv", ERROR_COLUMNS)):
                if name not in handles:
                    continue
                writer = csv.writer(handles[name], delimiter=";", lineterminator="\n")
                writer.writerow(columns)
                writers[name] = writer
            report = handles["report.html"]
            detail_links = ' · <a href="details.csv" download>Всички полета CSV</a> · <a href="errors.csv" download>Грешки CSV</a>' if include_details else ""
            report.write(_HTML_START.replace("__DETAIL_LINKS__", detail_links).replace("__MERGE_LINKS__", _merge_links(output_dir)))
            first = True
            for record in records:
                summary = profile_row(record)
                works = list(workplace_rows(record, summary))
                writers["profiles.csv"].writerow([safe_csv(summary[key]) for key in PROFILE_COLUMNS])
                counts["profiles"] += 1
                status = str(record.get("status", ""))
                profile_statuses[status] = profile_statuses.get(status, 0) + 1
                category = str(record.get("category", ""))
                category_stat = category_counts.setdefault(category, {"label": summary["Категория"], "exported": 0, "statuses": {}})
                category_stat["exported"] += 1
                category_stat["statuses"][status] = category_stat["statuses"].get(status, 0) + 1
                for row in works:
                    writers["workplaces.csv"].writerow([safe_csv(row[key]) for key in WORKPLACE_COLUMNS])
                    counts["workplaces"] += 1
                for path, kind, value, encoded in flatten_json(record) if include_details else ():
                    parts = path.split("/")
                    section = "/".join(parts[1:3]) if path.startswith("/sections/") else (parts[1] if len(parts) > 1 else "record")
                    row = [record.get("profile_id", ""), summary["Име"], section, path, kind, value, encoded, record.get("url", ""), record.get("fetched_at", "")]
                    # JSON strings start with a quote; JSON numbers are validated
                    # numeric values. Keep that column directly JSON-decodable.
                    writers["details.csv"].writerow([cell if index == 6 else safe_csv(cell) for index, cell in enumerate(row)])
                    counts["details"] += 1
                errors = record.get("errors") or []
                counts["errors"] += len(errors)
                for error in errors if include_details else ():
                    detail = error if isinstance(error, dict) else {"message": str(error)}
                    row = [record.get("profile_id", ""), summary["Име"], detail.get("section", detail.get("endpoint", detail.get("route", ""))), detail.get("message", detail.get("error", _json(error))), _json(error), record.get("url", ""), record.get("fetched_at", "")]
                    writers["errors.csv"].writerow([safe_csv(cell) for cell in row])
                if not first:
                    report.write(",\n")
                first = False
                report.write(_html_json({"p": summary, "w": [{key: value for key, value in row.items() if key != "JSON запис"} for row in works]}))
            for item in run_metadata["categories"]:
                item.update(category_counts.pop(str(item["category"]), {"label": {101: "Медицински експерти", 103: "Лечебни заведения"}.get(item["category"], str(item["category"])), "exported": 0, "statuses": {}}))
            run_metadata["categories"].extend(dict({"category": key, "expected": None, "discovered": value["exported"]}, **value) for key, value in category_counts.items())
            run_metadata["profile_statuses"] = profile_statuses
            report.write(_HTML_END.replace("__RUN_METADATA__", _html_json(run_metadata)))
        for name, temporary_path in temporary.items():
            os.replace(temporary_path, output_dir / name)
    finally:
        for temporary_path in temporary.values():
            if temporary_path.exists():
                temporary_path.unlink()
    return {"counts": counts, "files": {name: str(output_dir / name) for name in filenames}}


_HTML_START = """<!doctype html>
<html lang="bg"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CredoWeb · Профили и контакти</title>
<style>
:root{color-scheme:light;--ink:#16343b;--muted:#61747a;--line:#dce5e6;--brand:#126c70}
*{box-sizing:border-box}body{margin:0;background:#f3f6f5;color:var(--ink);font:15px/1.5 system-ui,sans-serif}
header{padding:32px 5vw 24px;background:#fff;border-bottom:1px solid var(--line)}h1{font-size:30px;margin:4px 0 8px;letter-spacing:-1px}
.eyebrow{color:var(--brand);font-size:12px;font-weight:700;letter-spacing:2px}.muted{color:var(--muted)}main{padding:24px 5vw}
.tools{display:flex;gap:12px;flex-wrap:wrap;align-items:end;margin-bottom:18px}label{display:grid;gap:5px;font-size:12px;font-weight:600}
input,select,button{font:inherit;border:1px solid #bfd0d0;border-radius:8px;padding:10px 12px;background:white;color:var(--ink)}input{width:360px;max-width:85vw}
button{cursor:pointer}button:hover{border-color:var(--brand)}button:disabled{opacity:.4;cursor:default}a{color:var(--brand)}
.meta{display:flex;gap:20px;justify-content:space-between;flex-wrap:wrap;margin:12px 0}.table-wrap{overflow:auto;background:white;border:1px solid var(--line);border-radius:10px}
.merge-downloads{padding:12px 16px;margin:12px 0;background:white;border:1px solid var(--line);border-radius:8px;line-height:1.8}
.coverage{padding:15px 18px;margin:0 0 22px;background:#e8f2ef;border:1px solid #c8ded7;border-radius:10px}.coverage p{margin:4px 0 0;font-size:14px}.coverage.sample,.coverage.partial,.coverage.interrupted,.coverage.failed{background:#fff5df;border-color:#ead39f}
.coverage-grid{display:flex;gap:16px;flex-wrap:wrap;margin:12px 0}.coverage-item{padding:10px 14px;background:#ffffffb3;border-radius:7px;min-width:235px}.coverage-item p{font-size:12px}.refresh-tools{display:flex;gap:15px;align-items:center;flex-wrap:wrap;margin-top:12px}.refresh-tools label{display:flex;gap:7px;align-items:center}.refresh-tools input{width:auto}.refresh-tools[hidden]{display:none}
table{border-collapse:collapse;width:100%;text-align:left;font-size:14px}th,td{padding:13px 16px;border-bottom:1px solid var(--line);vertical-align:top}th{background:#edf3f2;font-size:12px;white-space:nowrap}
tbody tr:hover{background:#f6faf9}.name-button{border:0;padding:0;text-align:left;background:transparent;color:var(--brand);font-weight:650}
.pager{display:flex;gap:12px;align-items:center;justify-content:end;margin:16px 0}.panel{background:white;border:1px solid var(--line);padding:24px;border-radius:12px;margin:22px 0}
.panel h2{margin-top:0}dl{display:grid;grid-template-columns:minmax(150px,1fr) 4fr;gap:10px 20px}dt{font-size:13px;color:var(--muted)}dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}
.details-head{display:flex;justify-content:space-between;gap:15px;align-items:start}.small{font-size:12px}.contact{white-space:pre-line;max-width:240px}.address{white-space:pre-line;min-width:240px;max-width:340px}
@media(max-width:650px){header{padding-top:22px}h1{font-size:25px}main{padding:18px 4vw}dl{grid-template-columns:1fr;gap:4px}dd{margin-bottom:12px}.panel{padding:16px}}
</style></head><body><header><div class="eyebrow">CREDOWEB / ПУБЛИЧЕН КАТАЛОГ</div><h1>Профили и контакти</h1>
<div class="muted">Лекари, медицински експерти и лечебни заведения. Изберете име, за да видите подробностите.</div></header>
<main><section id="coverage" class="coverage" aria-label="Обхват на извличането"><strong id="coverage-title">Събрани публични профили</strong><p id="coverage-stage"></p><p id="coverage-count"></p><p id="coverage-statuses"></p><div id="coverage-categories" class="coverage-grid"></div><p id="coverage-updated" class="muted small"></p>
<div id="refresh-tools" class="refresh-tools" hidden><button id="refresh-now">Обнови отчета</button><label><input id="auto-refresh" type="checkbox">Автоматично на 60 секунди</label><span id="refresh-note" class="small muted">Автоматичното обновяване изчаква, докато разглеждате подробности или използвате филтри.</span></div></section>
<div class="tools"><label>Търсене<input id="search" type="search" placeholder="Име, специалност, адрес, телефон…"></label>
<label>Категория<select id="category"><option value="">Всички категории</option></select></label><label>Тип профил<select id="profile-type"><option value="">Всички типове</option></select></label><label>Град<select id="city"><option value="">Всички градове</option></select></label>
<label>Статус<select id="status"><option value="">Всички статуси</option></select></label></div>
<div class="meta"><strong id="count" aria-live="polite"></strong><span class="small"><a href="profiles.csv" download>Профили CSV</a> · <a href="workplaces.csv" download>Адреси CSV</a>__DETAIL_LINKS__</span></div>
__MERGE_LINKS__
<div class="table-wrap"><table><thead><tr><th>Име / тип</th><th>Специалности</th><th>Град</th><th>Пълен адрес</th><th>Телефон</th><th>Имейл</th><th>Статус</th></tr></thead><tbody id="rows"></tbody></table></div>
<div class="pager"><button id="prev">← Назад</button><span id="page"></span><button id="next">Напред →</button></div>
<section id="detail" class="panel" hidden tabindex="-1"></section>
<p class="muted small">Профилите със статус „Само каталог“ съдържат намереното в каталога; техните подробности и празни контакти още не са проверени. При останалите профили празно поле означава, че стойност не е получена до момента. CSV файловете използват UTF-8 и разделител точка и запетая.</p><p id="details-note" class="muted small"></p></main>
<script id="records" type="application/json">[
"""

_HTML_END = r"""
]</script><script>
'use strict';
const data = JSON.parse(document.getElementById('records').textContent);
const $ = id => document.getElementById(id);
const run = __RUN_METADATA__;
const runLabels = {complete:'Завършено извличане',directory_complete:'Каталогът е събран; подробностите предстоят',partial:'Частично извличане',sample:'Ограничена извадка',interrupted:'Прекъснато извличане',failed:'Извличане с грешка',running:'Извличането продължава'};
const statusLabels = {complete:'Завършен',partial:'Частичен',unavailable:'Недостъпен',failed:'Грешка',listed:'Само каталог',pending:'Само каталог',in_progress:'Обогатява се'};
const stageLabels = {discover:'Откриване на профили в каталога',directory:'Каталог на откритите профили',enrich:'Допълване на подробности и контакти',completed:'Обработката е приключила'};
const fmt = value => Number(value || 0).toLocaleString('bg');
if($('merge-updated')) $('merge-updated').textContent = (run.merge_snapshot_at ? 'Обновени: '+new Date(run.merge_snapshot_at).toLocaleString('bg')+'. ' : '')+(run.merge_export_failed ? 'Последното обновяване е отложено. Показани са последно записаните CSV файлове; скриптът ще опита отново.' : 'Файловете се обновяват автоматично, докато скриптът работи.');
$('coverage-title').textContent = runLabels[run.status] || 'Събрани публични профили';
if(run.status) $('coverage').classList.add(run.status);
$('coverage-stage').textContent = stageLabels[run.stage] ? 'Етап: '+stageLabels[run.stage] : '';
$('coverage-count').textContent = fmt(data.length) + (Number.isInteger(run.expected) ? ' видими от общо ' + fmt(run.expected) + ' профила в избраните категории по данните на каталога.' : ' видими профила.');
const stats = run.profile_statuses || {};
const pendingCount = (stats.listed || 0) + (stats.pending || 0);
$('coverage-statuses').textContent = 'Завършени профили: '+fmt(stats.complete)+'. Частични: '+fmt(stats.partial)+'. В процес на допълване: '+fmt(stats.in_progress)+'. Само каталожни данни, подробностите предстоят: '+fmt(pendingCount)+'.'+((stats.unavailable || stats.failed) ? ' Недостъпни или с грешка: '+fmt((stats.unavailable || 0)+(stats.failed || 0))+'.' : '');
for(const category of run.categories || []) {
  const card=el('div',undefined,'coverage-item');card.append(el('strong',category.label));
  card.append(el('p','В отчета: '+fmt(category.exported)+(Number.isInteger(category.expected) ? ' от общо '+fmt(category.expected) : '')+'.'+(Number.isInteger(category.discovered) ? ' Открити: '+fmt(category.discovered)+'.' : '')));
  const s=category.statuses || {};card.append(el('p','Завършени: '+fmt(s.complete)+'. Частични: '+fmt(s.partial)+'. Очакват подробности: '+fmt((s.listed || 0)+(s.pending || 0))+'.'));$('coverage-categories').append(card);
}
$('coverage-updated').textContent = 'Последно обновяване на отчета: '+new Date(run.exported_at).toLocaleString('bg')+'.';
$('details-note').textContent = run.details_current ? 'Всички получени към това обновяване полета са в details.csv, а грешките — в errors.csv.' : 'Това е междинен отчет. Подробните файлове details.csv и errors.csv се обновяват при пълен експорт; налични по-стари версии не са включени сред връзките по-горе.';
$('refresh-tools').hidden = !run.running;
$('refresh-now').addEventListener('click',()=>location.reload());
const refreshStorageKey = 'credoweb-auto-refresh:'+location.pathname;
try { $('auto-refresh').checked = sessionStorage.getItem(refreshStorageKey)==='1'; } catch {}
$('auto-refresh').addEventListener('change',()=>{try { sessionStorage.setItem(refreshStorageKey,$('auto-refresh').checked ? '1' : '0'); } catch {}});
if(run.running) setInterval(()=>{
  if(!$('auto-refresh').checked) return;
  const usingFilters=['search','category','profile-type','city','status'].some(id=>$(id).value);
  if(usingFilters || !$('detail').hidden || document.hidden) return;
  location.reload();
},60000);
const fields = ['Име','Тип профил','Категория','Основни специалности','Други специалности','Град','Държава','Адреси','Телефони','Имейли','Уебсайтове','Длъжност','Описание','Услуги','Здравно осигуряване','Работно време','Източник','Извлечено на','Статус','Брой грешки','Налични секции'];
const searchIndex = data.map(item => Object.values(item.p).join(' ').toLocaleLowerCase('bg'));
let filtered = data.map((_, i) => i), page = 0;
const size = 100;
function el(tag, text, className) { const node = document.createElement(tag); if(text !== undefined) node.textContent = String(text); if(className) node.className = className; return node; }
function addOptions(id, key) { const totals=new Map();data.forEach(item=>{const value=item.p[key];if(value)totals.set(value,(totals.get(value)||0)+1);});[...totals.keys()].sort((a,b) => a.localeCompare(b,'bg')).forEach(value => {const label=key==='Статус' ? statusLabels[value] || value : value;const option=el('option',label+(key==='Тип профил' ? ' ('+fmt(totals.get(value))+')' : ''));option.value=value;$(id).append(option);}); }
addOptions('category','Категория');addOptions('profile-type','Тип профил');addOptions('city','Град');addOptions('status','Статус');
function sourceLink(value) { try { const url = new URL(value); if(!['https:','http:'].includes(url.protocol)) return el('span',value); const link=el('a',value);link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';return link; } catch { return el('span',value); } }
function definitions(container, row, keys) { const list=el('dl');keys.forEach(key => {const value=row[key];if(value === '' || value === undefined || value === null) return;list.append(el('dt',key));const body=el('dd');body.append(key==='Източник' || key==='Източник профил' ? sourceLink(value) : document.createTextNode(String(key==='Статус' ? statusLabels[value] || value : value)));list.append(body);});container.append(list); }
function show(index) { const item=data[index], panel=$('detail');panel.replaceChildren();panel.hidden=false;const head=el('div',undefined,'details-head');head.append(el('h2',item.p['Име'] || 'Профил '+item.p['ID']));const close=el('button','Затвори');close.addEventListener('click',() => {panel.hidden=true;});head.append(close);panel.append(head);if(['listed','pending'].includes(item.p['Статус']))panel.append(el('p','Това са данни от каталога. Подробностите и празните контакти още не са проверени.','coverage'));definitions(panel,item.p,['ID',...fields]);if(item.w.length){panel.append(el('h3','Месторабота, практики и адреси'));item.w.forEach(row=>{const group=el('details');group.append(el('summary',row['Наименование'] || row['Вид запис']));definitions(group,row,Object.keys(row).filter(key => !['ID профил','Име профил','JSON запис'].includes(key)));panel.append(group);});}panel.focus({preventScroll:true});panel.scrollIntoView({behavior:'smooth',block:'start'}); }
function render() { const body=$('rows');body.replaceChildren();const start=page*size;filtered.slice(start,start+size).forEach(index => {const p=data[index].p,tr=el('tr'),name=el('td'),button=el('button',p['Име'] || 'Профил '+p['ID'],'name-button');button.addEventListener('click',()=>show(index));name.append(button,el('div',p['Тип профил'],'small muted'));tr.append(name,el('td',[p['Основни специалности'],p['Други специалности']].filter(Boolean).join(' | ')),el('td',p['Град']),el('td',p['Адреси'],'address'),el('td',p['Телефони'],'contact'),el('td',p['Имейли'],'contact'),el('td',statusLabels[p['Статус']] || p['Статус'],'small'));body.append(tr);});if(!filtered.length){const row=el('tr'),cell=el('td','Няма съвпадащи профили.');cell.colSpan=7;row.append(cell);body.append(row);}$('count').textContent='В отчета: '+filtered.length.toLocaleString('bg')+' от '+data.length.toLocaleString('bg')+' профила';$('page').textContent=filtered.length ? (start+1)+'–'+Math.min(start+size,filtered.length) : '0';$('prev').disabled=page===0;$('next').disabled=start+size>=filtered.length; }
function filter() { const q=$('search').value.trim().toLocaleLowerCase('bg'),cat=$('category').value,profileType=$('profile-type').value,city=$('city').value,status=$('status').value;filtered=[];data.forEach((item,i)=>{const p=item.p;if((!q || searchIndex[i].includes(q)) && (!cat || p['Категория']===cat) && (!profileType || p['Тип профил']===profileType) && (!city || p['Град']===city) && (!status || p['Статус']===status))filtered.push(i);});page=0;render(); }
let timer;$('search').addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(filter,120);});['category','profile-type','city','status'].forEach(id=>$(id).addEventListener('change',filter));$('prev').addEventListener('click',()=>{page--;render();});$('next').addEventListener('click',()=>{page++;render();});render();
</script></body></html>
"""
