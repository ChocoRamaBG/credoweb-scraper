"""Discover public directory profiles without assuming search is unbounded.

CredoWeb's *API* pages are zero based (the browser displays one based pages).
Its page count is capped even when totalCount is larger.  Public filter facets
are used to search smaller overlapping partitions, then IDs are deduplicated.
Completeness is measured against the server's initial count, never inferred
merely from reaching the last page.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from typing import Any


def _number(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _facets(data: dict) -> dict[str, dict]:
    return {f["key"]: f for f in data.get("filterList", [])
            if isinstance(f, dict) and f.get("key")}


def _buckets(facet: dict, options_only: bool = False) -> list[tuple[str, int]]:
    allowed = {str(o.get("id")) for o in facet.get("options", [])}
    counts = facet.get("optionAggregationCount", {})
    return [(str(key), _number(count)) for key, count in counts.items()
            if str(key) != "0" and _number(count) > 0
            and (not options_only or str(key) in allowed)]


def _known_locations(profile_ids: set[str], known: dict[str, dict]) -> tuple[Counter, set[str], set[str]]:
    """Count distinct profiles tagged with public location IDs in listing data.

    A location may occur in basicInfo or practice/workplace addresses. Count
    each profile once per ID, even when several of its addresses share a city.
    City IDs with a known shortfall make better recovery queries than every
    region/country whose hierarchical membership is absent from the listing.
    """
    counts: Counter = Counter()
    cities: set[str] = set()
    countries: set[str] = set()

    for profile_id in profile_ids:
        locations: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"city", "location", "country", "region", "division"} and isinstance(child, dict):
                        identifier = child.get("id")
                        if identifier is not None and str(identifier) != "0":
                            identifier = str(identifier)
                            locations.add(identifier)
                            if key == "city":
                                cities.add(identifier)
                            elif key == "country":
                                countries.add(identifier)
                    if isinstance(child, (dict, list)):
                        visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(known.get(profile_id, {}))
        counts.update(locations)
    return counts, cities, countries


def discover(client: Any, categories: list[int], max_pages: int | None = None,
             on_item: Callable[[int, dict], None] | None = None,
             progress: Callable[[str], None] | None = None, *,
             on_checkpoint: Callable[[dict], None] | None = None,
             initial_items: Iterable[tuple[int, dict]] = (),
             max_recovery_requests: int | None = 200) -> dict:
    """Call ``client.get('search', params)`` and emit every distinct profile.

    ``client.get`` returns the API's already-unwrapped ``data`` object and is
    responsible for caching, delays and retries. A page limit intentionally
    samples each original category and does not create filter partitions.
    All requests are sequential. Primary partitions are exhaustive; optional
    overlapping recovery is bounded per category by max_recovery_requests.
    Cached recovery pages are free when client.get_cached is available. Pass
    None to exhaust every recovery partition. Existing listing records can be
    replayed through initial_items so later runs preserve all prior discoveries.
    Callbacks receive mutable progress snapshots; persist/copy them immediately.
    Callback exceptions propagate so a failed save is never silently completed.
    """
    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be positive or None")
    if max_recovery_requests is not None and max_recovery_requests < 0:
        raise ValueError("max_recovery_requests must be nonnegative or None")
    on_item = on_item or (lambda category, item: None)
    progress = progress or (lambda message: None)
    on_checkpoint = on_checkpoint or (lambda report: None)
    categories = list(dict.fromkeys(categories))
    report: dict[str, Any] = {
        "api_page_base": 0, "limited": max_pages is not None,
        "complete": False, "running": True, "categories": [], "partitions": [], "warnings": [],
        "max_recovery_requests": max_recovery_requests,
        "primary_request_errors": 0, "recovery_request_errors": 0, "traversal_complete": False,
    }
    category_reports = {category: {
        "category": category, "expected": 0, "expected_known": False,
        "discovered": 0, "missing": None, "complete": False,
        "limited": max_pages is not None, "status": "pending",
        "recovery_http_requests": 0, "recovery_budget_exhausted": False,
    } for category in categories}
    report["categories"] = list(category_reports.values())
    saved_items: dict[int, list[dict]] = {category: [] for category in categories}
    for category, item in initial_items:
        if category in saved_items:
            saved_items[category].append(item)
    for category, items in saved_items.items():
        category_reports[category]["discovered"] = len({str(item["profileId"]) for item in items
                                                       if item.get("profileId") is not None})
        # Include already cached category totals in the first checkpoint, even
        # when another category is processed first. This performs no HTTP calls.
        if callable(getattr(client, "get_cached", None)):
            cached = client.get_cached("search", {"cat": category, "page": 0})
            if (isinstance(cached, dict) and isinstance(cached.get("result"), list)
                    and "totalCount" in cached and _number(cached["totalCount"], -1) >= 0):
                total = _number(cached["totalCount"])
                category_reports[category].update(expected=total, expected_known=True,
                    missing=max(total - category_reports[category]["discovered"], 0))
    dropdown_cache: dict[str, list] = {}

    class RecoveryBudgetReached(Exception):
        pass

    def emit_checkpoint() -> None:
        report["expected"] = sum(c["expected"] for c in report["categories"])
        report["discovered"] = sum(c["discovered"] for c in report["categories"])
        report["complete"] = all(c["complete"] for c in report["categories"])
        on_checkpoint(report)

    emit_checkpoint()

    def warning(message: str) -> None:
        if message not in report["warnings"]:
            report["warnings"].append(message)
            progress(message)

    def dropdown(key: str) -> list:
        if key not in dropdown_cache:
            try:
                value = client.get("dropdown", {"key": key})
                dropdown_cache[key] = value if isinstance(value, list) else []
            except RuntimeError as exc:
                report["primary_request_errors"] += 1
                dropdown_cache[key] = []
                warning(f"Could not load public filter {key}: {exc}")
        return dropdown_cache[key]

    for category in categories:
        seen: set[str] = set()
        refreshed: set[str] = set()
        known: dict[str, dict] = {}
        completed_queries: dict[tuple, set[str]] = {}
        category_report = category_reports[category]
        expected = category_report["expected"]
        category_report["status"] = "running"
        deferred_recovery: list[tuple[dict, dict, set[str], dict]] = []

        def accept(data: dict, local: set[str], *, saved: bool = False) -> tuple[str, ...]:
            signature: list[str] = []
            for item in data.get("result", []):
                if not isinstance(item, dict):
                    continue
                if item.get("categoryId") is not None and _number(item["categoryId"], -1) != category:
                    warning(f"Category {category}: ignored result from category {item['categoryId']}")
                    continue
                profile_id = item.get("profileId")
                if profile_id is None:
                    warning(f"Category {category}: a search result has no profileId")
                    continue
                key = str(profile_id)
                signature.append(key)
                local.add(key)
                if key not in seen or (not saved and key not in refreshed):
                    on_item(category, item)
                    seen.add(key)
                    known[key] = item
                if not saved:
                    refreshed.add(key)
            category_report["discovered"] = len(seen)
            category_report["missing"] = max(expected - len(seen), 0) if category_report["expected_known"] else None
            return tuple(signature)

        accept({"result": saved_items.pop(category)}, set(), saved=True)
        emit_checkpoint()

        def fetch(params: dict, page: int, recovery: bool = False) -> dict | None:
            if callable(getattr(client, "check_budget", None)):
                client.check_budget()
            try:
                query = {"cat": category, **params, "page": page}
                data = None
                if recovery and callable(getattr(client, "get_cached", None)):
                    get_cached = getattr(client, "get_recovery_cached", client.get_cached)
                    data = get_cached("search", query)
                if data is None:
                    if (recovery and max_recovery_requests is not None
                            and category_report["recovery_http_requests"] >= max_recovery_requests):
                        category_report["recovery_budget_exhausted"] = True
                        raise RecoveryBudgetReached
                    before = getattr(client, "requests", None)
                    try:
                        data = client.get("search", query)
                    finally:
                        if recovery:
                            after = getattr(client, "requests", None)
                            category_report["recovery_http_requests"] += max(0, after - before) if isinstance(before, int) and isinstance(after, int) else 1
                if not isinstance(data, dict) or not isinstance(data.get("result"), list):
                    raise RuntimeError("Malformed search response")
                for key in ("totalCount", "pageCount"):
                    value = data.get(key)
                    if (isinstance(value, bool) or not isinstance(value, (int, str))
                            or not str(value).isdecimal()):
                        raise RuntimeError(f"Malformed search response: invalid {key}")
                total, pages = int(data["totalCount"]), int(data["pageCount"])
                if total > 0 and (pages == 0 or not data["result"]):
                    raise RuntimeError("Malformed search response: positive count without pages/results")
                if total < len(data["result"]) or (total == 0 and pages != 0):
                    raise RuntimeError("Malformed search response: inconsistent counts")
                if recovery and callable(getattr(client, "remember_recovery", None)):
                    client.remember_recovery("search", query)
                return data
            except RuntimeError as exc:
                report["recovery_request_errors" if recovery else "primary_request_errors"] += 1
                warning(f"Category {category}, filters {params}, API page {page}: {exc}")
                return None

        def walk(params: dict, seed: dict | None = None, *, split: bool = True,
                 recovery: bool = False) -> set[str]:
            query_key = tuple(sorted((str(k), str(v)) for k, v in params.items()))
            if query_key in completed_queries:
                return completed_queries[query_key]
            # A prior interrupted run may already have explored later fallback
            # queries. Preserve those IDs without making those network calls again.
            local: set[str] = set(seen) if not params else set()
            if set(params) == {"profileType"}:
                local = {key for key, item in known.items()
                         if str(((item.get("basicInfo") or {}).get("profileType") or {}).get("id")) == str(params["profileType"])}
            completed_queries[query_key] = local
            try:
                data = seed if seed is not None else fetch(params, 0, recovery)
            except RecoveryBudgetReached:
                return local
            if data is None:
                return local
            total = _number(data.get("totalCount"))
            pages = _number(data.get("pageCount"))
            signature = accept(data, local)
            size = len(data["result"])
            capped = total > pages * size if pages and size else total > 0
            entry = {"category": category, "filters": dict(params), "expected": total,
                     "page_count": pages, "pages_read": 1, "capped": capped,
                     "discovered": len(local), "complete": False, "recovery": recovery}
            report["partitions"].append(entry)
            facets = _facets(data)
            progress(f"Category {category}, filters {params or '{}'}: {total} profiles, {pages} pages")
            emit_checkpoint()

            def enough() -> bool:
                return len(local) >= total

            def child(key: str, value: str, *, recovery_child: bool = False) -> None:
                if not enough():
                    local.update(walk({**params, key: value}, split=False, recovery=recovery_child))

            def read_pages(*, recovery_pages: bool = False) -> None:
                prior_signatures = {signature} if signature else set()
                end = min(pages, max_pages) if max_pages is not None else pages
                for page in range(1, end):
                    if max_pages is None and enough():
                        break
                    try:
                        following = fetch(params, page, recovery or recovery_pages)
                    except RecoveryBudgetReached:
                        break
                    if following is None:
                        continue
                    entry["pages_read"] += 1
                    current = accept(following, local)
                    entry["discovered"] = len(local)
                    emit_checkpoint()
                    if not current:
                        warning(f"Category {category}, filters {params}: empty API page {page} before expected end")
                        break
                    if current in prior_signatures:
                        warning(f"Category {category}, filters {params}: repeated API page {page}; stopped this partition")
                        break
                    prior_signatures.add(current)
                    if page % 25 == 0:
                        progress(f"Category {category}: {len(seen)}/{expected or total} unique profiles; API page {page}/{pages - 1}")

            if capped and split and max_pages is None:
                # Profile types are the one mutually exclusive public facet.
                types = _buckets(facets.get("profileType", {}), options_only=True)
                if "profileType" not in params and len(types) > 1:
                    for value, count in sorted(types, key=lambda item: item[1]):
                        if enough():
                            break
                        local.update(walk({**params, "profileType": value}))

                # Do not repeat the entire category after all mutually exclusive
                # type partitions. A small gap in one type cannot justify another
                # thousands-page traversal of all other types.
                types_handled = "profileType" not in params and len(types) > 1
                if not enough() and not types_handled:
                    locations = dict(_buckets(facets.get("location", {})))
                    capacity = pages * max(size, 1)
                    # The public dropdown starts with Bulgaria's 28 regions.
                    # Take the IDs from the server; do not invent ID ranges.
                    regions = [str(o.get("id")) for o in dropdown("applicableLocations")
                               if isinstance(o, dict) and str(o.get("label", "")).startswith("Област ")]
                    countries = [str(o.get("id")) for o in dropdown("applicableCountries")
                                 if isinstance(o, dict)]
                    preferred = list(dict.fromkeys(regions + countries))
                    for value in preferred:
                        if 0 < locations.get(value, 0) <= capacity:
                            child("location", value)
                        if enough():
                            break

                    # Unfiltered pages also find profiles with missing facet
                    # values. gender=0 is ignored by the actual API, so it is
                    # intentionally never used as a supposed "missing" filter.
                    if not enough():
                        read_pages(recovery_pages=True)

                    # These are overlapping recovery searches. Stop as soon as
                    # the observed unique IDs cover the initial advertised count.
                    for key in ("gender", "speciality", "location"):
                        if enough() or category_report["recovery_budget_exhausted"]:
                            break
                        if key in params:
                            continue
                        candidates = _buckets(facets.get(key, {}), options_only=key != "location")
                        for value, count in sorted(candidates, key=lambda item: -item[1]):
                            if key == "location" and value in preferred:
                                continue
                            if 0 < count <= capacity:
                                child(key, value, recovery_child=True)
                            if enough() or category_report["recovery_budget_exhausted"]:
                                break
            else:
                read_pages()

            entry["discovered"] = len(local)
            entry["complete"] = len(local) >= total
            # Even an uncapped query can miss IDs when tied rankings shift
            # between pages. Defer small-gap recovery until primary traversal
            # has completed, and use the same bounded optional request budget.
            if not capped and split and max_pages is None and not enough():
                deferred_recovery.append((params, data, local, entry))
            emit_checkpoint()
            return local

        initial = fetch({}, 0)
        if initial is not None:
            expected = _number(initial.get("totalCount"))
            category_report.update(expected=expected, expected_known=True,
                                   missing=max(expected - len(seen), 0))
            emit_checkpoint()
            walk({}, initial)
            for params, data, local, entry in deferred_recovery:
                if category_report["recovery_budget_exhausted"]:
                    break
                facets = _facets(data)
                capacity = _number(data.get("pageCount")) * max(len(data.get("result", [])), 1)
                for key in ("gender", "speciality", "location"):
                    if key in params:
                        continue
                    candidates = _buckets(facets.get(key, {}), options_only=key != "location")
                    if key == "location":
                        location_counts, known_cities, known_countries = _known_locations(local, known)
                        candidates.sort(key=lambda item: (
                            0 if item[0] in known_cities else 2 if item[0] in known_countries else 1,
                            item[1]))
                    else:
                        candidates.sort(key=lambda item: item[1])
                    for value, count in candidates:
                        if len(local) >= entry["expected"] or category_report["recovery_budget_exhausted"]:
                            break
                        if count > capacity:
                            continue
                        if key == "location" and location_counts[value] >= count:
                            continue
                        if key == "gender":
                            matched = sum(str(((known.get(profile_id, {}).get("basicInfo") or {}).get("sex") or {}).get("id")) == value
                                          for profile_id in local)
                            if matched >= count:
                                continue
                        local.update(walk({**params, key: value}, split=False, recovery=True))
                        if key == "location":
                            location_counts, _, _ = _known_locations(local, known)
                    if len(local) >= entry["expected"] or category_report["recovery_budget_exhausted"]:
                        break
                entry.update(discovered=len(local), complete=len(local) >= entry["expected"])
                emit_checkpoint()
            for entry in report["partitions"]:
                if entry["category"] == category and not entry["filters"]:
                    entry.update(discovered=len(seen), complete=len(seen) >= expected)
        complete = initial is not None and len(seen) >= expected
        gap = max(expected - len(seen), 0)
        category_report.update(discovered=len(seen), missing=gap if initial is not None else None,
                               complete=complete, status="complete" if complete else "partial")
        if category_report["recovery_budget_exhausted"]:
            warning(f"Category {category}: optional recovery budget exhausted after {category_report['recovery_http_requests']} new HTTP requests; resume to continue from cache")
        if not complete:
            reason = "requested page limit" if max_pages is not None else "search/API coverage gap"
            warning(f"Category {category}: discovered {len(seen)}/{expected}; {reason}; missing {gap}")
        if len(seen) > expected:
            warning(f"Category {category}: count changed during crawl ({len(seen)} unique, initially {expected})")
        emit_checkpoint()
    report["running"] = False
    # Source totals can contain irreducible gaps. A coherent primary traversal
    # still needs a fresh weekly sweep; optional overlap recovery resumes from
    # its own persistent response set. Failed primary requests pin this sweep.
    report["traversal_complete"] = max_pages is None and not report["primary_request_errors"]
    emit_checkpoint()
    return report
