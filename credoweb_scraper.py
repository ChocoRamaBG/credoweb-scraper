#!/usr/bin/env python3
"""Collect unauthenticated CredoWeb profiles, with a resumable cache and table exports."""
from __future__ import annotations

import argparse
import email.utils
import json
import logging
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

BASE_URL = "https://www.credoweb.bg/api/"
LOG = logging.getLogger("credoweb")


class APIError(RuntimeError):
    def __init__(self, message: str, status: int = 0, url: str = ""):
        super().__init__(message)
        self.status = status
        self.url = url


class BudgetExpired(Exception):
    """A planned checkpoint, deliberately not an API/request failure."""
    def __init__(self, kind):
        self.kind = kind
        super().__init__(f"{kind} time budget reached")


def parsed_time(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError):
        return None


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if target.scheme != "https" or target.netloc != "www.credoweb.bg":
            raise APIError("Unexpected redirect outside CredoWeb", code, req.full_url)
        if not target.path.startswith("/api/"):
            raise APIError("API redirected to a non-API page (possibly login)", code, req.full_url)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(5):
        try:
            temporary.replace(path)
            return
        except OSError as exc:
            if attempt == 4 or getattr(exc, "winerror", None) not in (5, 32, 33):
                raise
            time.sleep(0.1 * 2 ** attempt)


class Client:
    """GET-only client. Stores data, never response tokens or login cookies."""

    def __init__(self, database: Path, delay=1.5, timeout=40.0, retries=3, *, refresh_days=None):
        self.db = sqlite3.connect(database)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS responses (
              url TEXT PRIMARY KEY, data TEXT NOT NULL, fetched_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS listings (
              profile_id INTEGER PRIMARY KEY, category INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS profiles (
              profile_id INTEGER PRIMARY KEY, category INTEGER NOT NULL,
              status TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS profile_attempts (
              profile_id INTEGER PRIMARY KEY, attempted_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS recovery_responses (url TEXT PRIMARY KEY);
        """)
        self.delay, self.timeout, self.retries = delay, timeout, retries
        self.last_request = 0.0
        self.requests = self.cache_hits = 0
        self.opener = build_opener(SafeRedirect())
        self.refresh_cutoff = (datetime.now(timezone.utc) - timedelta(days=refresh_days)
                               if refresh_days is not None else None)
        self.search_cutoff = self.refresh_cutoff
        self.profile_scope = None
        self.profile_cutoff = None
        self.deadlines = {}

    def check_budget(self, wait=0):
        if self.deadlines:
            kind, deadline = min(self.deadlines.items(), key=lambda item: item[1])
            if time.monotonic() + wait >= deadline:
                raise BudgetExpired(kind)

    def sleep(self, seconds):
        self.check_budget(seconds)
        time.sleep(seconds)

    @staticmethod
    def url(route: str, params: dict | None = None) -> str:
        if route.startswith("/api/"):
            route = route[5:]
        parsed = urlsplit(urljoin(BASE_URL, route))
        if parsed.scheme != "https" or parsed.netloc != "www.credoweb.bg":
            raise APIError("Refusing an API URL outside www.credoweb.bg")
        if not parsed.path.startswith("/api/") or ".." in parsed.path.split("/"):
            raise APIError("Refusing a URL outside the public API")
        values = dict(parse_qsl(parsed.query, keep_blank_values=True))
        values.update({k: v for k, v in (params or {}).items() if v is not None})
        values["context"] = "bg"
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                           urlencode(sorted(values.items()), doseq=True), ""))

    def get(self, route: str, params: dict | None = None):
        self.check_budget()
        url = self.url(route, params)
        hit = self.get_cached(route, params)
        if hit is not None:
            self.cache_hits += 1
            return hit
        for attempt in range(self.retries + 1):
            self.sleep(max(0.0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            self.requests += 1
            request = Request(url, headers={
                "User-Agent": "CredoWebPublicDirectoryExporter/1.0",
                "Accept": "application/json", "Accept-Language": "bg,en;q=0.5",
            }, method="GET")
            try:
                remaining = min(self.deadlines.values()) - time.monotonic() if self.deadlines else self.timeout
                with self.opener.open(request, timeout=max(0.01, min(self.timeout, remaining))) as response:
                    payload = json.load(response)
                if not isinstance(payload, dict) or "data" not in payload:
                    raise APIError("Unexpected API response: missing data", url=url)
                if payload.get("error") or payload.get("errors"):
                    raise APIError("API returned an error payload", url=url)
                data = payload["data"]
                if not isinstance(data, (dict, list)):
                    raise APIError("Unexpected API data type", url=url)
                self.db.execute("INSERT OR REPLACE INTO responses VALUES (?,?,?)",
                                (url, json.dumps(data, ensure_ascii=False), utc_now()))
                self.db.commit()
                return data
            except HTTPError as exc:
                # Authentication/authorization failures are never retried or bypassed.
                if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == self.retries:
                    raise APIError(f"HTTP {exc.code}: {url}", exc.code, url) from exc
                wait = self._retry_delay(exc.headers.get("Retry-After"), attempt)
                LOG.warning("HTTP %s; retry in %.1fs: %s", exc.code, wait, url)
                self.sleep(wait)
            except (URLError, TimeoutError, OSError, ValueError) as exc:
                if attempt == self.retries:
                    raise APIError(f"Request failed ({type(exc).__name__}): {url}", url=url) from exc
                wait = min(60, 2 ** (attempt + 1))
                LOG.warning("%s; retry in %ss", type(exc).__name__, wait)
                self.sleep(wait)
        raise AssertionError("Unreachable retry state")

    def get_cached(self, route: str, params: dict | None = None):
        url = self.url(route, params)
        hit = self.db.execute("SELECT data,fetched_at FROM responses WHERE url=?", (url,)).fetchone()
        path = urlsplit(url).path
        cutoff = self.search_cutoff if path in ("/api/search", "/api/dropdown") else self.refresh_cutoff
        if self.profile_scope is not None and (path == self.profile_scope or path.startswith(self.profile_scope + "/")):
            cutoff = self.profile_cutoff
            module = dict(parse_qsl(urlsplit(url).query)).get("module")
            if self.refresh_cutoff is not None and (path.endswith("/businessCard") or module in ("about", "tabContacts", "subNavigation")):
                cutoff = max(cutoff, self.refresh_cutoff) if cutoff else self.refresh_cutoff
        if hit and (cutoff is None or (parsed_time(hit[1]) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff):
            return json.loads(hit[0])
        return None

    def get_recovery_cached(self, route, params=None):
        """Old overlap pages stay reusable while primary directory pages refresh."""
        cached = self.get_cached(route, params)
        if cached is not None:
            return cached
        row = self.db.execute("""SELECT r.data FROM responses r JOIN recovery_responses q
            ON q.url=r.url WHERE r.url=?""", (self.url(route, params),)).fetchone()
        return json.loads(row[0]) if row else None

    def remember_recovery(self, route, params=None):
        self.db.execute("INSERT OR IGNORE INTO recovery_responses VALUES (?)", (self.url(route, params),))
        self.db.commit()

    @staticmethod
    def _retry_delay(value, attempt):
        if value:
            try:
                return max(0.0, float(value))
            except ValueError:
                try:
                    target = email.utils.parsedate_to_datetime(value)
                    return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
        return min(60, 2 ** (attempt + 1))

    def add_listing(self, category, item):
        profile_id = item.get("profileId")
        if not isinstance(profile_id, int) or isinstance(profile_id, bool):
            raise APIError("Search result has no numeric profileId")
        encoded = json.dumps(item, ensure_ascii=False)
        changed = self.db.execute("""INSERT INTO listings VALUES (?,?,?)
            ON CONFLICT(profile_id) DO UPDATE SET category=excluded.category,data=excluded.data
            WHERE listings.data != excluded.data OR listings.category != excluded.category""",
            (profile_id, category, encoded)).rowcount
        self.db.commit()

    def records(self):
        # Discovery already contains useful identity, practice and contact data.
        # Do not hide these rows until slow detail/feed collection has finished.
        fetched_at = self.db.execute("SELECT MAX(fetched_at) FROM responses").fetchone()[0] or utc_now()
        for profile_id, category, listing_data, profile_data in self.db.execute("""
                SELECT l.profile_id,l.category,l.data,p.data
                FROM listings l LEFT JOIN profiles p ON p.profile_id=l.profile_id
                ORDER BY l.category,l.profile_id"""):
            listing = json.loads(listing_data)
            if profile_data:
                record = json.loads(profile_data)
                record["listing"] = listing
            else:
                basic = listing.get("basicInfo") or {}
                prefix = "page" if listing.get("profileType") == "page" else "profile"
                record = {"profile_id": profile_id, "category": category,
                          "url": f"https://www.credoweb.bg/{prefix}/{profile_id}/{basic.get('slug', '')}",
                          "listing": listing, "sections": {}, "fetched_at": fetched_at,
                          "status": "listed", "errors": []}
            yield record
        for (data,) in self.db.execute("""SELECT p.data FROM profiles p LEFT JOIN listings l
                                        ON l.profile_id=p.profile_id WHERE l.profile_id IS NULL
                                        ORDER BY p.category,p.profile_id"""):
            yield json.loads(data)

    def save_profile(self, record):
        self.db.execute("INSERT OR REPLACE INTO profiles VALUES (?,?,?,?)",
                        (record["profile_id"], record["category"], record["status"],
                         json.dumps(record, ensure_ascii=False)))
        self.db.commit()

    def close(self):
        self.db.close()


def arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=Path("output"), help="Result and checkpoint directory")
    p.add_argument("--categories", nargs="+", type=int, choices=(101, 103), default=[101, 103],
                   help="101=medical experts; 103=hospitals/healthcare facilities")
    p.add_argument("--max-pages", type=int, help="SAMPLE: first N search pages per category")
    p.add_argument("--max-profiles", type=int, help="SAMPLE: enrich at most N profiles; all discovered directory rows remain visible")
    p.add_argument("--max-section-pages", type=int, help="SAMPLE: limit pages per profile section")
    p.add_argument("--delay", type=float, default=1.5, help="Minimum seconds between HTTP request starts")
    p.add_argument("--timeout", type=float, default=40, help="HTTP timeout in seconds")
    p.add_argument("--retries", type=int, default=3, help="Retries for transient failures")
    p.add_argument("--export-only", action="store_true", help="Rebuild exports from checkpoint without network")
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--directory-only", action="store_true", help="Collect and publish all directory rows; skip detail enrichment")
    modes.add_argument("--enrich-only", action="store_true", help="Resume detailed profiles from the existing directory")
    p.add_argument("--quick-export", action="store_true", help="Export summary CSV/HTML without the large field-by-field file")
    p.add_argument("--export-interval", type=float, default=120, help="Seconds between incremental summary exports")
    p.add_argument("--recovery-requests", type=int, default=200, help="New requests per category for overlapping gap recovery; -1 = unlimited")
    p.add_argument("--refresh-days", type=float, help="Refresh responses and completed profiles older than N days; resume unfinished refresh sweeps")
    p.add_argument("--max-runtime-seconds", type=float, help="Gracefully checkpoint and export after this collection time budget (exports may take longer)")
    p.add_argument("--discovery-budget-seconds", type=float, help="Time for directory discovery before continuing profile enrichment")
    p.add_argument("--profile-budget-seconds", type=float, help="Time per profile before rotating to the next; defaults to 120 in a time-bounded run")
    opts = p.parse_args(argv)
    for key in ("max_pages", "max_profiles", "max_section_pages"):
        if getattr(opts, key) is not None and getattr(opts, key) < 1:
            p.error(f"--{key.replace('_', '-')} must be at least 1")
    if opts.delay < 0.5 or opts.timeout <= 0 or opts.retries < 0:
        p.error("delay must be >=0.5; timeout >0; retries >=0")
    if opts.export_interval < 5 or opts.recovery_requests < -1:
        p.error("export-interval must be >=5; recovery-requests must be >=-1")
    for key in ("refresh_days", "max_runtime_seconds", "discovery_budget_seconds", "profile_budget_seconds"):
        value = getattr(opts, key)
        if value is not None and (not math.isfinite(value) or value <= 0):
            p.error(f"--{key.replace('_', '-')} must be finite and greater than zero")
    if opts.max_runtime_seconds and opts.profile_budget_seconds is None:
        opts.profile_budget_seconds = 120
    opts.categories = sorted(set(opts.categories))
    return opts


def selected_listings(client, categories, maximum):
    # Sample both categories instead of consuming the entire sample on doctors.
    groups = [client.db.execute("SELECT data FROM listings WHERE category=? ORDER BY rowid", (cat,))
              for cat in categories]
    count = 0
    for bundle in zip_longest(*groups):
        for category, row in zip(categories, bundle):
            if row is not None:
                yield category, json.loads(row[0])
                count += 1
                if maximum and count >= maximum:
                    return


def enrichment_listings(client, categories, maximum):
    """Alternate new/unfinished profiles and due refreshes, oldest attempt first.

    Materialize the queues before writes, so recording an attempt cannot change
    the active SQLite cursor or repeatedly select a profile in the same run.
    """
    groups = []
    cutoff = client.refresh_cutoff.isoformat() if client.refresh_cutoff else None
    for category in categories:
        for complete in (False, True):
            if complete and cutoff is None:
                continue
            condition = ("p.status='complete' AND COALESCE(json_extract(p.data,'$.fetched_at'),'') < ?"
                         if complete else "(p.status IS NULL OR p.status!='complete')")
            params = (category, cutoff) if complete else (category,)
            groups.append(client.db.execute(f"""
                SELECT l.category,l.data FROM listings l
                LEFT JOIN profiles p ON p.profile_id=l.profile_id
                LEFT JOIN profile_attempts a ON a.profile_id=l.profile_id
                WHERE l.category=? AND {condition}
                ORDER BY COALESCE(a.attempted_at,''),l.profile_id""", params).fetchall())
    count = 0
    for bundle in zip_longest(*groups):
        for row in bundle:
            if row is not None:
                yield row[0], json.loads(row[1])
                count += 1
                if maximum and count >= maximum:
                    return


def begin_discovery_refresh(client):
    """Persist a sweep boundary: weekly limits must not refetch page one forever."""
    if client.refresh_cutoff is None:
        return
    row = client.db.execute("SELECT value FROM settings WHERE key='discovery_refresh'").fetchone()
    state = json.loads(row[0]) if row else {}
    completed = parsed_time(state.get("completed_at"))
    if not state or (completed is not None and completed < client.refresh_cutoff):
        state = {"cutoff": client.refresh_cutoff.isoformat(), "started_at": utc_now()}
        client.db.execute("INSERT OR REPLACE INTO settings VALUES ('discovery_refresh',?)", (json.dumps(state),))
        client.db.commit()
    client.search_cutoff = parsed_time(state.get("cutoff")) or client.refresh_cutoff


def finish_discovery_refresh(client):
    if client.refresh_cutoff is None:
        return
    row = client.db.execute("SELECT value FROM settings WHERE key='discovery_refresh'").fetchone()
    state = json.loads(row[0]) if row else {}
    state["completed_at"] = utc_now()
    client.db.execute("INSERT OR REPLACE INTO settings VALUES ('discovery_refresh',?)", (json.dumps(state),))
    client.db.commit()


def prioritize_facility_cards(client, listings, save_record, progress=LOG.info):
    """Save facility addresses before traversing any profile's long content feeds.

    The business card contains the facility's public full address and structured
    location. Existing cards and completed profiles need no extra request. This
    pass intentionally leaves records incomplete for normal enrichment, which
    reuses the response cache and retries any card failures.
    """
    for category, listing in listings:
        client.check_budget()
        if category != 103:
            continue
        profile_id = listing["profileId"]
        saved = client.db.execute("SELECT data FROM profiles WHERE profile_id=?", (profile_id,)).fetchone()
        record = json.loads(saved[0]) if saved else None
        if record and (record.get("status") == "complete" or "businessCard" in record.get("sections", {})):
            continue
        if record is None:
            basic = listing.get("basicInfo") or {}
            record = {"profile_id": profile_id, "category": category,
                      "url": f"https://www.credoweb.bg/page/{profile_id}/{basic.get('slug', '')}",
                      "listing": listing, "sections": {}, "fetched_at": utc_now(),
                      "status": "in_progress", "errors": []}
        else:
            record["listing"] = listing
        route = f"profile/{profile_id}/businessCard"
        progress("Facility address: %s (%s)", listing.get("basicInfo", {}).get("title", ""), profile_id)
        try:
            card = client.get(route)
            if not isinstance(card, dict):
                raise APIError("Expected an object in the facility businessCard data field", url=client.url(route))
        except APIError as exc:
            failure = {"route": route, "kind": "access_denied" if exc.status in (401, 403) else "request_failed",
                       "message": str(exc), "status": exc.status}
            if failure not in record.setdefault("errors", []):
                record["errors"].append(failure)
            progress("Facility address unavailable for %s; normal enrichment will retry: %s", profile_id, exc)
        else:
            record.setdefault("sections", {})["businessCard"] = card
        save_record(record)


class RunLock:
    """A process-lifetime OS lock; an old lock file is harmless after a crash."""
    def __init__(self, path):
        self.file = open(path, "a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"\0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise APIError("Another collector is already using this output directory")
        self.file.seek(1)
        self.file.truncate()
        self.file.write(json.dumps({"pid": os.getpid(), "started_at": utc_now()}).encode())
        self.file.flush()

    def close(self):
        self.file.close()


def main(argv=None):
    opts = arguments(argv)
    output = opts.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(output / "crawl.log", encoding="utf-8")])
    from credoweb_discovery import discover
    from credoweb_profiles import fetch_profile
    from credoweb_export import export_records
    from credoweb_merge import export_merge_records

    if (opts.export_only or opts.enrich_only) and not (output / "checkpoint.sqlite3").exists():
        LOG.error("No checkpoint.sqlite3 in %s; collect the directory first", output)
        return 1
    try:
        lock = RunLock(output / "run.lock")
    except APIError as exc:
        LOG.error("%s", exc)
        return 1
    client = Client(output / "checkpoint.sqlite3", opts.delay, opts.timeout, opts.retries,
                    refresh_days=opts.refresh_days)
    if opts.max_runtime_seconds and not opts.export_only:
        client.deadlines["run"] = time.monotonic() + opts.max_runtime_seconds
    config = json.dumps(opts.categories)
    old = client.db.execute("SELECT value FROM settings WHERE key='categories'").fetchone()
    if old and old[0] != config and not opts.export_only:
        LOG.error("This output directory uses other categories; choose another --output directory")
        client.close()
        lock.close()
        return 1

    previous = {}
    if (output / "manifest.json").exists():
        previous = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    manifest = {"started_at": previous.get("started_at", utc_now()), "resumed_at": utc_now(),
                "source": "https://www.credoweb.bg/search?cat=100&page=1", "categories": opts.categories,
                "limits": {k: getattr(opts, k) for k in ("max_pages", "max_profiles", "max_section_pages")},
                "status": "running", "stage": "enrich" if opts.enrich_only else "discover",
                "refresh_days": opts.refresh_days,
                "budgets": {k: getattr(opts, k) for k in ("max_runtime_seconds", "discovery_budget_seconds", "profile_budget_seconds")},
                "pid": os.getpid(), "scope": "Every discovered directory profile is exported immediately. "
                "Listed rows have search-directory data; in_progress rows contain saved detail sections. "
                "Public advertised profile sections are enriched separately. "
                "Media/external websites are linked, not downloaded; article bodies are not crawled."}
    if previous.get("discovery"):
        manifest["discovery"] = previous["discovery"]
    if opts.export_only and previous:
        manifest = previous
        if manifest.get("export_status") == "failed" and manifest.get("crawl_status"):
            manifest["status"] = manifest["crawl_status"]
        manifest["exported_at"] = utc_now()

    last_export = 0.0
    last_state = 0.0
    exit_code = 0

    def update_counts():
        categories = dict(client.db.execute("SELECT category,COUNT(*) FROM listings GROUP BY category"))
        statuses = dict(client.db.execute("SELECT status,COUNT(*) FROM profiles GROUP BY status"))
        discovered = sum(categories.values())
        matched = client.db.execute("SELECT COUNT(*) FROM profiles p JOIN listings l ON l.profile_id=p.profile_id").fetchone()[0]
        orphan = client.db.execute("SELECT COUNT(*) FROM profiles p LEFT JOIN listings l ON l.profile_id=p.profile_id WHERE l.profile_id IS NULL").fetchone()[0]
        listed = discovered - matched
        if listed:
            statuses["listed"] = listed
        by_type = dict(client.db.execute("""SELECT COALESCE(json_extract(data,'$.basicInfo.profileType.label'),'Unknown'),COUNT(*)
                                             FROM listings GROUP BY json_extract(data,'$.basicInfo.profileType.label')"""))
        manifest["counts"] = {"discovered_unique_profiles": discovered,
                              "exported_profiles": discovered + orphan,
                              "detailed_profiles": matched + orphan,
                              "pending_details": listed + statuses.get("in_progress", 0),
                              "profile_statuses": statuses, "by_category": categories, "by_type": by_type,
                              "http_requests_this_run": client.requests, "cache_hits_this_run": client.cache_hits}
        # Bootstrap metadata from cache when recovering a run which never wrote a manifest.
        if not manifest.get("discovery"):
            category_rows = []
            for category in opts.categories:
                seed = client.get_cached("search", {"cat": category, "page": 0}) or {}
                expected = seed.get("totalCount")
                category_rows.append({"category": category, "expected": expected,
                                      "discovered": categories.get(category, 0), "complete": False})
            manifest["discovery"] = {"complete": False, "categories": category_rows,
                "expected": sum(r["expected"] or 0 for r in category_rows),
                "discovered": discovered}
        manifest["updated_at"] = utc_now()

    def publish(*, full=False, required=False):
        nonlocal last_export
        from credoweb_full import SEAL_FILENAME, seal_full_exports
        # A failed export must not leave an old seal reusable when the normalized
        # fields happened to stay identical (for example, only biography changed).
        (output / SEAL_FILENAME).unlink(missing_ok=True)
        update_counts()
        save_json(output / "manifest.json", manifest)
        failures = []
        # Both exports run on every checkpoint. A locked file in one format
        # must not prevent the other format from receiving the latest records.
        try:
            manifest["merge_exports"] = export_merge_records(
                client.records(), output / "merge", collection_status=manifest["status"])
        except OSError as exc:
            manifest["merge_export_status"] = "failed"
            manifest["merge_export_error"] = str(exc)
            failures.append(("Structured CSV", exc))
        else:
            manifest["merge_export_status"] = "complete"
            manifest.pop("merge_export_error", None)
        save_json(output / "manifest.json", manifest)
        try:
            manifest["exports"] = export_records(client.records(), output, include_details=full)
        except OSError as exc:
            manifest["report_export_status"] = "failed"
            failures.append(("Report", exc))
        else:
            manifest["report_export_status"] = "complete"
        if not failures:
            # Bind both exports before collection can advance to another record.
            try:
                seal_full_exports(output, output / "merge")
            except OSError as exc:
                failures.append(("Export snapshot seal", exc))
        last_export = time.monotonic()
        if failures:
            manifest["export_status"] = "failed"
            manifest["export_error"] = "; ".join(f"{name}: {error}" for name, error in failures)
            save_json(output / "manifest.json", manifest)
            if required:
                raise failures[0][1]
            LOG.warning("Export update delayed; collection continues and will retry: %s", manifest["export_error"])
            return
        manifest["export_status"] = "complete"
        manifest["export_mode"] = "full" if full else "summary"
        manifest.pop("export_error", None)
        save_json(output / "manifest.json", manifest)
        last_export = time.monotonic()
        LOG.info("Published %s directory rows (%s detail records): %s", manifest["counts"]["exported_profiles"],
                 manifest["counts"]["detailed_profiles"], output / "report.html")
        LOG.info("Structured CSV updated: %s", output / "merge" / "profiles.csv")

    def checkpoint(discovery_report=None):
        nonlocal last_state
        if discovery_report is not None:
            manifest["discovery"] = discovery_report
        now = time.monotonic()
        if now - last_state < 10:
            return
        update_counts()
        save_json(output / "manifest.json", manifest)
        last_state = now
        if time.monotonic() - last_export >= opts.export_interval:
            publish()

    def save_profile(record):
        client.save_profile(record)
        save_json(output / "raw" / "profiles" / f"{record['profile_id']}.json", record)
        if time.monotonic() - last_export >= opts.export_interval:
            checkpoint()

    try:
        if not opts.export_only:
            client.db.execute("INSERT OR REPLACE INTO settings VALUES ('categories',?)", (config,))
            client.db.commit()
            publish()  # Recover all previously discovered rows before any network work.
            if not opts.enrich_only:
                begin_discovery_refresh(client)
                # A refresh must actually revisit directory pages, even when
                # previously known IDs already cover the advertised total.
                initial_items = () if opts.refresh_days is not None else (
                    (cat, json.loads(data)) for cat, data in client.db.execute(
                        "SELECT category,data FROM listings ORDER BY category,profile_id"))
                if opts.discovery_budget_seconds:
                    client.deadlines["discovery"] = time.monotonic() + opts.discovery_budget_seconds
                try:
                    manifest["discovery"] = discover(client, sorted(opts.categories, reverse=True),
                        max_pages=opts.max_pages, on_item=client.add_listing, progress=LOG.info,
                        on_checkpoint=checkpoint, initial_items=initial_items,
                        max_recovery_requests=None if opts.recovery_requests == -1 else opts.recovery_requests)
                    if not opts.max_pages and manifest["discovery"].get("traversal_complete", manifest["discovery"].get("complete", False)):
                        finish_discovery_refresh(client)
                except BudgetExpired as exc:
                    if exc.kind != "discovery":
                        raise
                    manifest.setdefault("discovery", {}).update(complete=False, running=False, budget_exhausted=True)
                    LOG.info("Discovery time budget reached; saved pages will resume in the next run.")
                finally:
                    client.deadlines.pop("discovery", None)
                publish()
            if not opts.directory_only:
                manifest["stage"] = "enrich"
                save_json(output / "manifest.json", manifest)
                prioritize_facility_cards(client,
                    selected_listings(client, opts.categories, opts.max_profiles), save_profile)
                select = enrichment_listings if opts.refresh_days is not None or opts.max_runtime_seconds else selected_listings
                for index, (category, item) in enumerate(select(client, opts.categories, opts.max_profiles), 1):
                    client.check_budget()
                    profile_id = item["profileId"]
                    saved = client.db.execute("SELECT data FROM profiles WHERE profile_id=?", (profile_id,)).fetchone()
                    old_record = json.loads(saved[0]) if saved else None
                    if old_record and old_record.get("status") == "complete":
                        fetched = parsed_time(old_record.get("fetched_at"))
                        if client.refresh_cutoff is None or (fetched is not None and fetched >= client.refresh_cutoff):
                            continue
                    client.db.execute("INSERT OR REPLACE INTO profile_attempts VALUES (?,?)", (profile_id, utc_now()))
                    client.db.commit()
                    client.profile_scope = f"/api/profile/{profile_id}"
                    client.profile_cutoff = (parsed_time(old_record.get("refresh_cache_since"))
                        if old_record and old_record.get("status") != "complete" else client.refresh_cutoff)
                    if opts.profile_budget_seconds:
                        client.deadlines["profile"] = time.monotonic() + opts.profile_budget_seconds
                    LOG.info("Profile %s: %s (%s)", index, item.get("basicInfo", {}).get("title", ""), profile_id)
                    try:
                        record = fetch_profile(client, item, category, max_section_pages=opts.max_section_pages,
                                               on_section=save_profile, previous_record=old_record)
                        save_profile(record)
                    except BudgetExpired as exc:
                        if exc.kind != "profile":
                            raise
                        manifest["profiles_deferred_by_budget"] = manifest.get("profiles_deferred_by_budget", 0) + 1
                        LOG.info("Profile %s saved for later; moving to the next profile.", profile_id)
                    finally:
                        client.deadlines.pop("profile", None)
                        client.profile_scope = None
                        client.profile_cutoff = None
    except BudgetExpired as exc:
        LOG.info("Collection time budget reached. Saving the checkpoint and current CSV exports.")
        manifest["status"] = "budget_exhausted"
        manifest["budget_exhausted"] = exc.kind
    except KeyboardInterrupt:
        LOG.warning("Interrupted. All directory rows and profile sections already received are saved.")
        manifest["status"] = "interrupted"
        exit_code = 130
    except Exception as exc:
        LOG.exception("Collection failed: %s", exc)
        manifest["status"] = "failed"
        manifest["fatal_error"] = str(exc)
        exit_code = 1
    finally:
        try:
            update_counts()
            if manifest["status"] == "running":
                limited = any(manifest.get("limits", {}).values())
                discovery_complete = manifest.get("discovery", {}).get("complete", False)
                partial = any(s != "complete" and n for s, n in manifest["counts"]["profile_statuses"].items())
                if limited:
                    manifest["status"] = "sample"
                elif opts.directory_only:
                    manifest["status"] = "directory_complete" if discovery_complete else "partial"
                else:
                    manifest["status"] = "complete" if discovery_complete and not partial else "partial"
            manifest["stage"] = "directory" if opts.directory_only else ("completed" if manifest["status"] == "complete" else manifest.get("stage", "enrich"))
            manifest["finished_at"] = utc_now()
            full = not opts.quick_export and not opts.directory_only and manifest["status"] not in ("interrupted", "failed")
            # Avoid multi-gigabyte field dumps while most rows await their profile pages.
            if manifest["counts"]["profile_statuses"].get("listed", 0):
                full = False
            publish(full=full, required=True)
            LOG.info("Done: %s; %s profiles; %s", manifest["status"], manifest["counts"]["exported_profiles"], output)
            if manifest["status"] in ("failed", "partial") and not exit_code:
                exit_code = 2
        except Exception as exc:
            LOG.error("Export failed: %s. Data remains in checkpoint.sqlite3.", exc)
            manifest["crawl_status"] = manifest["status"]
            manifest["status"] = "failed"
            manifest["export_status"] = "failed"
            manifest["export_error"] = str(exc)
            try:
                save_json(output / "manifest.json", manifest)
            except OSError:
                LOG.error("Could not save export failure to manifest.json")
            exit_code = 1
        finally:
            client.close()
            lock.close()
    return exit_code

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
