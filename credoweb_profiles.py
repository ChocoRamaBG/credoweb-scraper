"""Read the public sections advertised by a CredoWeb profile.

The HTTP client supplies ``get(route, params=None)`` and returns the response's
``data`` value. No cookies, credentials, mutations, media downloads, follower
graph traversal, or article-body traversal are required by this module.
"""

from __future__ import annotations

from datetime import datetime, timezone
from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Callable, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit


def _objects(value: Any) -> Iterator[dict]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _safe_route(route: str, profile_id: int) -> tuple[str, dict[str, str]]:
    """Limit navigation metadata to GET sections of this exact public profile."""
    parsed = urlsplit(route)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise ValueError("Profile section has an external or unsupported route")
    path = parsed.path.lstrip("/")
    if path.startswith("api/"):
        path = path[4:]
    if not re.fullmatch(rf"profile/{profile_id}(?:/[A-Za-z][A-Za-z0-9]*)?", path):
        raise ValueError("Profile section does not belong to this profile")
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("Profile section contains duplicate query parameters")
    params = dict(pairs)
    params.pop("context", None)
    params.pop("lang", None)  # Profile endpoints reject the search API's lang field.
    # These account-only modules are never needed for a public profile export.
    if params.get("module", "").lower() in {
        "settings", "registration", "registrationfinish", "gdpr",
        "contactlist", "headernavigation", "administratedpage",
    }:
        raise ValueError("Account-only section is outside the public export")
    return path, params


def _section_key(path: str, params: dict[str, Any]) -> str:
    module = str(params.get("module") or path.rsplit("/", 1)[-1])
    extra = {key: value for key, value in params.items() if key != "module"}
    return module + ("?" + urlencode(sorted(extra.items())) if extra else "")


def _page_fingerprint(data: dict) -> str | None:
    # Restrict this to actual result arrays; UI filter lists repeat on every page.
    for key in ("contentList", "relatedPagesList", "result", "affiliateList"):
        rows = data.get(key)
        if isinstance(rows, list):
            return hashlib.sha256(json.dumps(rows, sort_keys=True,
                                            ensure_ascii=False).encode()).hexdigest()
    return None


def fetch_profile(client: Any, listing: dict, category: int,
                  max_section_pages: int | None = None, *,
                  on_section: Callable[[dict], None] | None = None,
                  previous_record: dict | None = None) -> dict:
    """Return every accessible advertised section, with explicit partial status.

    Publication/event/discussion sections retain all available paginated cards,
    metadata and links. Their linked article bodies and files are outside scope.
    Child structure/affiliate modal records are included, but linked profiles are
    not recursively crawled. Authentication failures remain explicit errors.

    About and contact sections are collected before potentially long content
    feeds. ``on_section(record)`` may persist the current record after every
    section/page or recorded error and once more after final status is known.
    Intermediate records have status ``in_progress`` and must be retried on
    resume. The callback runs synchronously and must not mutate the record;
    persistence failures propagate to the caller. Cached responses still pass
    through the callback, so an interrupted profile can be reconstructed.
    Previously saved sections remain available during a refresh or a failed
    request. Only a fully successful traversal removes obsolete sections.
    ``retained_sections`` identifies older sections not yet received this time.
    """
    profile_id = _positive_int(listing.get("profileId"))
    if profile_id is None:
        raise ValueError("listing.profileId must be a positive integer")
    if max_section_pages is not None and max_section_pages < 1:
        raise ValueError("max_section_pages must be positive or None")
    basic = listing.get("basicInfo") or {}
    slug = basic.get("slug") or listing.get("slug") or ""
    kind = listing.get("profileType")
    if isinstance(kind, dict):
        kind = kind.get("type")
    prefix = "page" if kind == "page" else "profile"
    record = {
        "profile_id": profile_id,
        "category": category,
        "url": f"https://www.credoweb.bg/{prefix}/{profile_id}/{slug}",
        "listing": listing,
        "sections": deepcopy((previous_record or {}).get("sections", {})),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "status": "in_progress",
        "errors": [],
    }
    sections = record["sections"]
    errors = record["errors"]
    visited: set[tuple[str, tuple]] = set()
    received: set[str] = set()
    if previous_record and previous_record.get("status") == "complete":
        record["last_successful_fetch_at"] = previous_record.get("fetched_at")
    elif previous_record and previous_record.get("last_successful_fetch_at"):
        record["last_successful_fetch_at"] = previous_record["last_successful_fetch_at"]
    if getattr(client, "profile_scope", None) is not None:
        cutoff = getattr(client, "profile_cutoff", None)
        record["refresh_cache_since"] = cutoff.isoformat() if cutoff else None

    def checkpoint() -> None:
        record["retained_sections"] = sorted(set(sections) - received)
        if on_section is not None:
            on_section(record)

    def error(route: str, kind: str, message: str, status: int | None = None) -> None:
        item = {"route": route, "kind": kind, "message": message}
        if status is not None:
            item["status"] = status
        errors.append(item)
        checkpoint()

    def get(path: str, params: dict[str, Any]) -> dict | None:
        request_key = (path, tuple(sorted(params.items())))
        if request_key in visited:
            return sections.get(_section_key(path, params))
        visited.add(request_key)
        route = path + ("?" + urlencode(params) if params else "")
        try:
            value = client.get(path, params or None)
        except RuntimeError as exc:
            status = getattr(exc, "status", None)
            kind = "access_denied" if status in (401, 403) else "request_failed"
            error(route, kind, str(exc), status)
            return None
        if not isinstance(value, dict):
            error(route, "invalid_response", "Expected an object in the API data field")
            return None
        sections[_section_key(path, params)] = value
        received.add(_section_key(path, params))
        checkpoint()
        return value

    def fetch_pages(path: str, params: dict[str, Any]) -> None:
        first = get(path, params)
        if first is None:
            return
        current = first
        # Profile subsection pages are zero-based. The response's ``page`` is
        # the cursor for the NEXT request (omitted/page=0 returns page=1).
        # Search pagination is handled separately by the directory crawler.
        page = int(params.get("page", 0))
        fetched = 1
        fingerprints = set()
        fingerprint = _page_fingerprint(first)
        if fingerprint:
            fingerprints.add(fingerprint)
        while True:
            total_pages = _positive_int(current.get("pageCount")) or _positive_int(current.get("pagesCount"))
            last_flag = current.get("isLastPage")
            next_page = _positive_int(current.get("page")) or page + 1
            more = next_page < total_pages if total_pages is not None else last_flag is False
            if last_flag is True:
                more = False
            if not more:
                return
            if (max_section_pages is not None and fetched >= max_section_pages) or fetched >= 10000:
                error(path, "page_limit", f"Section {_section_key(path, params)} stopped after {fetched} page(s); more pages are advertised")
                return
            if next_page <= page:
                error(path, "invalid_page_cursor", f"Section {_section_key(path, params)} returned a non-advancing page cursor")
                return
            page = next_page
            page_params = dict(params, page=page)
            current = get(path, page_params)
            if current is None:
                return
            fetched += 1
            fingerprint = _page_fingerprint(current)
            if fingerprint and fingerprint in fingerprints:
                error(path, "repeated_page", f"Section {_section_key(path, params)} repeated results at page {page}")
                return
            if fingerprint:
                fingerprints.add(fingerprint)

    path = f"profile/{profile_id}"
    get(path + "/businessCard", {})
    navigation = get(path, {"module": "subNavigation"})
    if navigation is not None:
        if not isinstance(navigation.get("navigation"), list):
            error(path, "invalid_navigation", "Missing navigation array; section discovery is incomplete")
        advertised_sections = []
        for node in _objects(navigation.get("navigation", [])):
            backend_route = node.get("backendRoute")
            if not isinstance(backend_route, str) or not backend_route:
                continue
            try:
                section_path, params = _safe_route(backend_route, profile_id)
            except ValueError as exc:
                error(backend_route, "unsupported_route", str(exc))
                continue
            advertised_sections.append((section_path, params))
        priorities = {"about": 0, "tabContacts": 1}
        advertised_sections.sort(key=lambda section: priorities.get(
            _section_key(*section).split("?", 1)[0], 2))
        for section_path, params in advertised_sections:
            if (section_path, tuple(sorted(params.items()))) not in visited:
                fetch_pages(section_path, params)

    # Frontend structure/affiliate dialogs expose these detail routes for entry
    # nodes. Do not treat team members or arbitrary linked IDs as child entries.
    details: set[tuple[str, int]] = set()
    for key in sorted(received):
        value = sections[key]
        for field, module in (("structureList", "structure"), ("affiliateList", "affiliate")):
            for node in _objects(value.get(field, [])):
                entry_id = _positive_int(node.get("profileId"))
                profile_type = node.get("profileType") or {}
                if entry_id and entry_id != profile_id and isinstance(profile_type, dict) and profile_type.get("type") == "entry":
                    details.add((module, entry_id))
    for module, entry_id in sorted(details):
        get(path, {"module": module, "entryId": entry_id})

    record["status"] = ("partial" if sections else "unavailable") if errors else "complete"
    if not errors:
        for key in set(sections) - received:
            del sections[key]
        record["last_successful_fetch_at"] = record["fetched_at"]
    checkpoint()
    return record
