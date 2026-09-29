"""Versioned detailed CSV publication alongside the normalized merge tables.

The collector seals both completed exports before another collection step can
change its records. Publication consumes that seal; it must never infer that
two files belong together merely because their profile IDs happen to match.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import urlsplit

from credoweb_export import PROFILE_COLUMNS, WORKPLACE_COLUMNS


FULL_COLUMNS = {"profiles": PROFILE_COLUMNS, "workplaces": WORKPLACE_COLUMNS}
FULL_FILES = ("full/profiles.csv.gz", "full/workplaces.csv.gz", "full/manifest.json")
SEAL_FILENAME = "export_snapshot.json"
MAX_GIT_FILE_BYTES = 100 * 1024 * 1024


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def file_identity(path: Path) -> dict:
    return {"bytes": path.stat().st_size, "sha256": digest(path)}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def seal_full_exports(output: Path, normalized: Path) -> dict:
    """Called only after both export functions finish over the same records.

This small local seal is not published. Failed/locked exports must not refresh
it; its exact hashes make a previous seal unusable for a newer partial export.
"""
    manifest_path = normalized / "manifest.json"
    before = manifest_path.read_bytes()
    manifest = json.loads(before)
    if manifest.get("publication_status") != "complete":
        raise ValueError("Cannot seal an incomplete normalized snapshot")
    seal = {"schema_version": "1.0", "snapshot_at": manifest["snapshot_at"],
            "normalized_manifest_sha256": hashlib.sha256(before).hexdigest(),
            "files": {name: file_identity(output / (name + ".csv")) for name in FULL_COLUMNS}}
    if manifest_path.read_bytes() != before:
        raise ValueError("Normalized snapshot changed while sealing detailed exports")
    _write_json(output / SEAL_FILENAME, seal)
    return seal


def _profile_url_id(value: str) -> str:
    try:
        parsed = urlsplit(value)
        match = re.fullmatch(r"/profile/([0-9]+)(?:/[^/]+)?/?", parsed.path)
        if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"credoweb.bg", "www.credoweb.bg"}
                or parsed.username or parsed.password or parsed.port not in {None, 80, 443} or not match):
            raise ValueError("Detailed CSV has an invalid profile source URL")
        return match.group(1)
    except ValueError as error:
        raise ValueError("Detailed CSV has an invalid profile source URL") from error


def _read_rows(stream, name: str, profile_ids: set[str]) -> tuple[int, set[str]]:
    # A workplace's retained JSON evidence can exceed csv's small default limit.
    old_limit = csv.field_size_limit()
    csv.field_size_limit(max(old_limit, 64 * 1024 * 1024))
    try:
        reader = csv.DictReader(stream, delimiter=";")
        if reader.fieldnames != FULL_COLUMNS[name]:
            raise ValueError(f"Detailed CSV column mismatch: {name}")
        seen = set()
        count = 0
        for row in reader:
            count += 1
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Malformed detailed CSV row: {name}")
            key = row["ID" if name == "profiles" else "ID профил"]
            url = row["Източник" if name == "profiles" else "Източник профил"]
            if not re.fullmatch(r"[1-9][0-9]*", key) or _profile_url_id(url) != key:
                raise ValueError(f"Detailed CSV profile ID/source URL mismatch: {name}")
            if name == "profiles":
                if key in seen:
                    raise ValueError("Duplicate detailed profile ID")
                seen.add(key)
            elif key not in profile_ids:
                raise ValueError("Unknown parent profile in detailed workplaces")
        return count, seen
    finally:
        csv.field_size_limit(old_limit)


def prepare_full_bundle(output: Path, normalized: Path) -> dict:
    """Compress a sealed, stopped collector export into NORMALIZED/full.

The manifest is replaced last. Publication subsequently stages and validates
all normalized and detailed files together before creating one Git commit.
"""
    normalized_bytes = (normalized / "manifest.json").read_bytes()
    manifest = json.loads(normalized_bytes)
    if manifest.get("publication_status") != "complete" or manifest.get("collection_status") == "running":
        raise ValueError("Stop collection and complete both CSV exports before preparing publication")
    seal_path = output / SEAL_FILENAME
    if not seal_path.exists():
        raise ValueError("Detailed export snapshot seal is missing; regenerate both exports with the collector")
    seal_bytes = seal_path.read_bytes()
    seal = json.loads(seal_bytes)
    expected_hash = hashlib.sha256(normalized_bytes).hexdigest()
    if (seal.get("schema_version") != "1.0" or seal.get("normalized_manifest_sha256") != expected_hash
            or seal.get("snapshot_at") != manifest["snapshot_at"]):
        raise ValueError("Detailed export seal does not match the normalized snapshot")
    target = normalized / "full"
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".full-staging-", dir=normalized) as temporary:
        staging = Path(temporary)
        full = {"schema_version": "1.0", "publication_status": "complete", "encoding": "UTF-8 BOM",
                "delimiter": ";", "snapshot_at": manifest["snapshot_at"],
                "normalized_manifest_sha256": expected_hash, "counts": {}, "files": {}}
        for name, columns in FULL_COLUMNS.items():
            source = output / (name + ".csv")
            expected = seal.get("files", {}).get(name)
            if not expected or file_identity(source) != expected:
                raise ValueError(f"Detailed export differs from its snapshot seal: {name}")
            compressed = staging / (name + ".csv.gz")
            with source.open("rb") as incoming, compressed.open("wb") as destination:
                # Both the timestamp and stored filename otherwise make bytes
                # change even when the actual CSV content is identical.
                with gzip.GzipFile(filename="", fileobj=destination, mode="wb", compresslevel=6, mtime=0) as archive:
                    shutil.copyfileobj(incoming, archive, length=1024 * 1024)
            if file_identity(source) != expected:
                raise ValueError(f"Detailed export changed while compressing: {name}")
            if compressed.stat().st_size >= MAX_GIT_FILE_BYTES:
                raise ValueError(f"Detailed compressed file exceeds GitHub's 100 MiB limit: {name}")
            with source.open(encoding="utf-8-sig", newline="") as stream:
                count, _ = _read_rows(stream, name, _normalized_ids(normalized))
            full["counts"][name] = count
            full["files"][name] = {"filename": compressed.name, "columns": list(columns), "rows": count,
                                   **file_identity(compressed), "uncompressed_bytes": expected["bytes"],
                                   "uncompressed_sha256": expected["sha256"]}
        _write_json(staging / "manifest.json", full)
        validate_full_bundle(normalized, full_folder=staging)
        if (seal_path.read_bytes() != seal_bytes or (normalized / "manifest.json").read_bytes() != normalized_bytes
                or any(file_identity(output / (name + ".csv")) != seal["files"][name] for name in FULL_COLUMNS)):
            raise ValueError("CSV snapshot changed while preparing detailed publication")
        for name in FULL_COLUMNS:
            (staging / (name + ".csv.gz")).replace(target / (name + ".csv.gz"))
        (staging / "manifest.json").replace(target / "manifest.json")
    return full


def _normalized_ids(normalized: Path) -> set[str]:
    with (normalized / "profiles.csv").open(encoding="utf-8-sig", newline="") as stream:
        return {row["profile_id"] for row in csv.DictReader(stream)}


def validate_full_bundle(normalized: Path, *, full_folder: Path | None = None) -> dict:
    """Validate compressed bytes, CSV bytes/schema/joins and exact snapshot link."""
    full_folder = full_folder or normalized / "full"
    manifest = json.loads((full_folder / "manifest.json").read_text(encoding="utf-8"))
    normalized_bytes = (normalized / "manifest.json").read_bytes()
    parent = json.loads(normalized_bytes)
    if (manifest.get("schema_version") != "1.0" or manifest.get("publication_status") != "complete"
            or manifest.get("encoding") != "UTF-8 BOM" or manifest.get("delimiter") != ";"):
        raise ValueError("Unsupported or incomplete detailed publication manifest")
    if (manifest.get("normalized_manifest_sha256") != hashlib.sha256(normalized_bytes).hexdigest()
            or manifest.get("snapshot_at") != parent["snapshot_at"]):
        raise ValueError("Detailed publication belongs to a different normalized snapshot")
    if set(manifest.get("files", {})) != set(FULL_COLUMNS):
        raise ValueError("Detailed publication is missing a required CSV")
    ids = _normalized_ids(normalized)
    if not ids:
        raise ValueError("Refusing to publish an empty detailed profile directory")
    for name, columns in FULL_COLUMNS.items():
        entry = manifest["files"][name]
        if entry.get("filename") != name + ".csv.gz" or entry.get("columns") != columns:
            raise ValueError(f"Unexpected detailed file name or columns: {name}")
        path = full_folder / entry["filename"]
        if path.stat().st_size >= MAX_GIT_FILE_BYTES or file_identity(path) != {key: entry[key] for key in ("bytes", "sha256")}:
            raise ValueError(f"Detailed compressed hash/size mismatch: {name}")
        raw_digest = hashlib.sha256()
        size = 0
        try:
            with gzip.open(path, "rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    size += len(block)
                    if size > entry["uncompressed_bytes"]:
                        raise ValueError(f"Detailed uncompressed size mismatch: {name}")
                    raw_digest.update(block)
            if size != entry["uncompressed_bytes"] or raw_digest.hexdigest() != entry["uncompressed_sha256"]:
                raise ValueError(f"Detailed uncompressed hash/size mismatch: {name}")
            with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as stream:
                count, seen = _read_rows(stream, name, ids)
        except (gzip.BadGzipFile, EOFError) as error:
            raise ValueError(f"Invalid detailed gzip file: {name}") from error
        if count != entry["rows"] or count != manifest["counts"][name]:
            raise ValueError(f"Detailed CSV row count mismatch: {name}")
        if name == "profiles" and seen != ids:
            raise ValueError("Detailed profile IDs do not match the normalized snapshot")
    return manifest
