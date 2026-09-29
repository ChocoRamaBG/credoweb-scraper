#!/usr/bin/env python3
"""Persist collector state and publish validated CSV snapshots on GitHub.

Uses Python's standard library and the GitHub CLI (already on hosted runners).
No token is ever put in a command argument or a Git remote URL.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

TABLES = ("profiles", "workplaces", "contacts", "specialties")
BUNDLE_FILES = tuple(table + ".csv" for table in TABLES) + ("schema.json", "README.md", "manifest.json")
ASSET_PATTERN = re.compile(r"^checkpoint-[A-Za-z0-9-]+\.sqlite3\.gz$")
MAX_ASSET_BYTES = 2 * 1024 ** 3


def gh_executable() -> str:
    return os.environ.get("GH_BIN", "gh")


def run(*args: str, cwd: Path | None = None, env: dict | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, env=env, check=True, capture_output=True,
                            text=True, encoding="utf-8")
    return result.stdout.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_bundle(folder: Path) -> dict:
    """Reject incomplete publication, changed files, malformed rows and broken joins."""
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("publication_status") != "complete":
        raise ValueError("CSV publication is not complete")
    schema = json.loads((folder / "schema.json").read_text(encoding="utf-8"))
    rows = {}
    for name in TABLES:
        entry = manifest["files"][name]
        filename = name + ".csv"
        if entry["filename"] != filename:
            raise ValueError(f"Unexpected CSV filename for {name}")
        path = folder / filename
        if path.stat().st_size != entry["bytes"] or sha256(path) != entry["sha256"]:
            raise ValueError(f"CSV hash/size mismatch: {filename}")
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != schema["tables"][name]["columns"]:
                raise ValueError(f"CSV column mismatch: {filename}")
            rows[name] = list(reader)
        if len(rows[name]) != entry["rows"] or len(rows[name]) != manifest["counts"][name]:
            raise ValueError(f"CSV row count mismatch: {filename}")
        keys = set()
        for row in rows[name]:
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Malformed CSV row: {filename}")
            key = tuple(row[column] for column in schema["tables"][name]["primary_key"])
            if key in keys or not any(key):
                raise ValueError(f"Duplicate or empty primary key: {filename}")
            keys.add(key)
    if not rows["profiles"]:
        raise ValueError("Refusing to publish an empty profile directory")
    profile_ids = {row["profile_id"] for row in rows["profiles"]}
    workplace_owners = {row["workplace_key"]: row["profile_id"] for row in rows["workplaces"]}
    for name in TABLES[1:]:
        if any(row["profile_id"] not in profile_ids for row in rows[name]):
            raise ValueError(f"Unknown parent profile in {name}")
    if any(row["workplace_key"] and workplace_owners.get(row["workplace_key"]) != row["profile_id"]
           for row in rows["contacts"]):
        raise ValueError("Unknown or cross-profile workplace in contacts")
    for filename in ("schema.json", "README.md"):
        entry = manifest["support_files"][filename]
        path = folder / filename
        if entry["filename"] != filename or path.stat().st_size != entry["bytes"] or sha256(path) != entry["sha256"]:
            raise ValueError(f"Support file hash/size mismatch: {filename}")
    return manifest


def validate_database(path: Path) -> None:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as database:
        if database.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Checkpoint SQLite integrity check failed")
        names = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"listings", "profiles", "responses", "settings"}.issubset(names):
            raise ValueError("Checkpoint is missing collector tables")


def database_profile_ids(database: Path) -> set[str]:
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        return {str(row[0]) for row in connection.execute("SELECT profile_id FROM listings UNION SELECT profile_id FROM profiles")}


def snapshot_database(database: Path, archive: Path) -> set[str]:
    """Online SQLite backup is safe even if the local collector is still writing."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="credoweb-state-") as temporary:
        snapshot = Path(temporary) / "checkpoint.sqlite3"
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(snapshot)) as target:
                source.backup(target, pages=256, sleep=0.05)
        validate_database(snapshot)
        identifiers = database_profile_ids(snapshot)
        temporary_archive = archive.with_name(archive.name + ".tmp")
        try:
            with snapshot.open("rb") as source, gzip.open(temporary_archive, "wb", compresslevel=6) as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            if temporary_archive.stat().st_size >= MAX_ASSET_BYTES:
                raise ValueError("Compressed checkpoint exceeds GitHub's 2 GiB per-asset limit")
            temporary_archive.replace(archive)
        finally:
            temporary_archive.unlink(missing_ok=True)
    return identifiers


def restore_archive(archive: Path, database: Path) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    temporary = database.with_name(database.name + ".restore")
    try:
        with gzip.open(archive, "rb") as source, temporary.open("wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        validate_database(temporary)
        temporary.replace(database)
    finally:
        temporary.unlink(missing_ok=True)


class GitHub:
    def __init__(self, repository: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Repository must be OWNER/NAME")
        self.repository = repository
        self.token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not self.token:
            try:
                self.token = run(gh_executable(), "auth", "token")
            except (OSError, subprocess.CalledProcessError) as error:
                raise ValueError("Authenticate with gh auth login or set GH_TOKEN for GitHub access") from error
        self.base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")

    def api(self, path: str, *, method: str = "GET", data: dict | None = None, missing_ok: bool = False):
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "CredoWeb-collector"}
        payload = None
        if data is not None:
            payload = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        url = f"{self.base}/repos/{self.repository}"
        if path:
            url += "/" + path.lstrip("/")
        request = Request(url, data=payload, headers=headers, method=method)
        try:
            with urlopen(request, timeout=120) as response:
                body = response.read()
                return json.loads(body) if body else None
        except HTTPError as error:
            if error.code == 404 and missing_ok:
                return None
            raise RuntimeError(f"GitHub API {method} {path} failed: HTTP {error.code}") from error

    def release(self):
        # Verify access first: private-repository permission failures also return 404.
        self.api("")
        return self.api("releases/tags/checkpoint", missing_ok=True)


def checkpoint_assets(release: dict) -> list[dict]:
    return sorted((asset for asset in release.get("assets", [])
                   if ASSET_PATTERN.fullmatch(asset.get("name", "")) and asset.get("state") == "uploaded"),
                  key=lambda asset: (asset.get("created_at", ""), asset["id"]), reverse=True)


def restore_state(github: GitHub, database: Path) -> bool:
    if database.exists():
        raise ValueError("Restore destination already exists; refusing to overwrite local state")
    release = github.release()
    if release is None:
        if github.api("branches/data", missing_ok=True) is not None:
            raise RuntimeError("Data branch exists but checkpoint release is missing; restore the saved state before running")
        print("No checkpoint or data branch exists: starting the first collection")
        return False
    assets = checkpoint_assets(release)
    if not assets:
        raise RuntimeError("Checkpoint release exists without a completed database asset; refusing an empty restart")
    asset = assets[0]
    with tempfile.TemporaryDirectory(prefix="credoweb-download-") as temporary:
        run(gh_executable(), "release", "download", "checkpoint", "--repo", github.repository,
            "--pattern", asset["name"], "--dir", temporary)
        archive = Path(temporary) / asset["name"]
        if archive.stat().st_size != asset["size"]:
            raise ValueError("Downloaded checkpoint size mismatch")
        digest = asset.get("digest")
        if digest and digest.startswith("sha256:") and sha256(archive) != digest.removeprefix("sha256:"):
            raise ValueError("Downloaded checkpoint hash mismatch")
        restore_archive(archive, database)
    print(f"Restored {asset['name']}")
    return True


def save_state(github: GitHub, database: Path, repository: Path | None = None) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"checkpoint-{timestamp}-{uuid.uuid4().hex[:12]}.sqlite3.gz"
    with tempfile.TemporaryDirectory(prefix="credoweb-upload-") as temporary:
        archive = Path(temporary) / filename
        identifiers = snapshot_database(database, archive)
        if not identifiers:
            raise ValueError("Refusing to save an empty collector database")
        if repository is not None:
            _, previous_ids = remote_data_state(repository)
            assert_no_profile_loss(identifiers, previous_ids)
        release = github.release()
        if release is None:
            repository = github.api("")
            release = github.api("releases", method="POST", data={
                "tag_name": "checkpoint", "target_commitish": repository["default_branch"],
                "name": "Collector checkpoint", "prerelease": True, "make_latest": "false",
                "body": "Persistent collector database. CSV consumers should use the data branch. The newest two snapshots are retained."})
        run(gh_executable(), "release", "upload", "checkpoint", str(archive), "--repo", github.repository)
        uploaded = github.release()
        assets = checkpoint_assets(uploaded)
        matching = [asset for asset in assets if asset["name"] == filename]
        if not matching or matching[0]["size"] != archive.stat().st_size:
            raise RuntimeError("Uploaded checkpoint could not be verified; retaining all previous assets")
        digest = matching[0].get("digest")
        if digest and digest.startswith("sha256:") and sha256(archive) != digest.removeprefix("sha256:"):
            raise RuntimeError("Uploaded checkpoint hash mismatch; retaining all previous assets")
        for old in assets[2:]:
            github.api(f"releases/assets/{old['id']}", method="DELETE")
    print(f"Saved {filename}; previous checkpoint retained")
    return filename


def profile_ids(csv_text: str) -> set[str]:
    return {row["profile_id"] for row in csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))}


def remote_data_state(repository: Path, remote: str = "origin") -> tuple[str | None, set[str]]:
    result = subprocess.run(["git", "ls-remote", "--exit-code", remote, "refs/heads/data"],
                            cwd=repository, capture_output=True, text=True, encoding="utf-8")
    if result.returncode not in (0, 2):
        raise RuntimeError("Cannot inspect remote data branch: " + result.stderr.strip())
    if result.returncode == 2:
        return None, set()
    run("git", "fetch", "--no-tags", remote, "refs/heads/data", cwd=repository)
    parent = run("git", "rev-parse", "FETCH_HEAD", cwd=repository)
    previous = run("git", "show", f"{parent}:profiles.csv", cwd=repository)
    return parent, profile_ids(previous)


def assert_no_profile_loss(current: set[str], previous: set[str]) -> None:
    missing = previous - current
    if missing:
        raise ValueError(f"New snapshot loses {len(missing)} existing profiles; refusing publication")


def validate_collection(folder: Path, database: Path, repository: Path) -> dict:
    manifest = validate_bundle(folder)
    current = profile_ids((folder / "profiles.csv").read_text(encoding="utf-8-sig"))
    if current != database_profile_ids(database):
        raise ValueError("CSV profile IDs do not match the saved collector database")
    _, previous = remote_data_state(repository)
    assert_no_profile_loss(current, previous)
    return manifest


def publish_data(folder: Path, repository: Path, remote: str = "origin") -> str:
    """Build one data-branch commit without changing the source checkout or index."""
    with tempfile.TemporaryDirectory(prefix="credoweb-publish-") as temporary:
        staging = Path(temporary)
        first_manifest = (folder / "manifest.json").read_bytes()
        for filename in BUNDLE_FILES:
            shutil.copyfile(folder / filename, staging / filename)
        if (folder / "manifest.json").read_bytes() != first_manifest:
            raise ValueError("CSV snapshot changed during staging; retry publication")
        manifest = validate_bundle(staging)
        parent, previous_ids = remote_data_state(repository, remote)
        current = (staging / "profiles.csv").read_text(encoding="utf-8-sig")
        assert_no_profile_loss(profile_ids(current), previous_ids)
        environment = os.environ.copy()
        environment["GIT_INDEX_FILE"] = str(staging / "publication.index")
        environment.setdefault("GIT_AUTHOR_NAME", "github-actions[bot]")
        environment.setdefault("GIT_AUTHOR_EMAIL", "41898282+github-actions[bot]@users.noreply.github.com")
        environment.setdefault("GIT_COMMITTER_NAME", environment["GIT_AUTHOR_NAME"])
        environment.setdefault("GIT_COMMITTER_EMAIL", environment["GIT_AUTHOR_EMAIL"])
        run("git", "read-tree", "--empty", cwd=repository, env=environment)
        for filename in BUNDLE_FILES:
            blob = run("git", "hash-object", "-w", "--no-filters", str(staging / filename), cwd=repository)
            run("git", "update-index", "--add", "--cacheinfo", f"100644,{blob},{filename}",
                cwd=repository, env=environment)
        tree = run("git", "write-tree", cwd=repository, env=environment)
        if parent and run("git", "rev-parse", f"{parent}^{{tree}}", cwd=repository) == tree:
            print("CSV snapshot is unchanged")
            return parent
        arguments = ["git", "commit-tree", tree]
        if parent:
            arguments += ["-p", parent]
        arguments += ["-m", f"Refresh CSV database ({manifest['snapshot_at']})"]
        commit = run(*arguments, cwd=repository, env=environment)
        run("git", "push", remote, f"{commit}:refs/heads/data", cwd=repository)
        print(f"Published {manifest['counts']['profiles']} profiles to data branch: {commit}")
        return commit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("restore", "save", "publish", "validate", "snapshot"))
    parser.add_argument("--repository", "--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="GitHub OWNER/NAME")
    parser.add_argument("--output", type=Path, default=Path("output"), help="Collector output directory")
    parser.add_argument("--database", type=Path, help="Override OUTPUT/checkpoint.sqlite3")
    parser.add_argument("--csv-dir", type=Path, help="Override OUTPUT/merge")
    parser.add_argument("--source-dir", type=Path, default=Path.cwd())
    parser.add_argument("--archive", type=Path, help="Destination for the local snapshot command")
    args = parser.parse_args(argv)
    args.database = args.database or args.output / "checkpoint.sqlite3"
    args.csv_dir = args.csv_dir or args.output / "merge"
    if args.command == "restore":
        restore_state(GitHub(args.repository), args.database)
    elif args.command == "save":
        save_state(GitHub(args.repository), args.database, args.source_dir)
    elif args.command == "publish":
        publish_data(args.csv_dir, args.source_dir)
    elif args.command == "validate":
        print(json.dumps(validate_collection(args.csv_dir, args.database, args.source_dir)["counts"]))
    elif args.command == "snapshot":
        if not args.archive:
            parser.error("snapshot requires --archive")
        snapshot_database(args.database, args.archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
