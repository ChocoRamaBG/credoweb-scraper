"""Offline integration checks for durable state and the downstream CSV branch."""
import csv
import gzip
import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from credoweb_merge import export_merge_records
from scripts import github_sync


def record(profile_id=123):
    return {"profile_id": profile_id, "category": 101, "status": "complete", "errors": [],
            "url": f"https://www.credoweb.bg/profile/{profile_id}/example",
            "sections": {"businessCard": {"title": "Д-р Пример", "email": "office@example.bg"}}}


def create_database(path):
    database = sqlite3.connect(path)
    database.executescript("""
        CREATE TABLE listings(profile_id INTEGER PRIMARY KEY, data TEXT);
        CREATE TABLE profiles(profile_id INTEGER PRIMARY KEY, data TEXT);
        CREATE TABLE responses(url TEXT PRIMARY KEY, data TEXT);
        CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO listings VALUES(123, '{"name":"example"}');
    """)
    database.commit()
    return database


class GitHubRequestTests(unittest.TestCase):
    def test_repository_access_check_uses_canonical_endpoint(self):
        # GitHub returns 404 for /repos/OWNER/REPO/ even with a valid token.
        with patch.dict("os.environ", {"GH_TOKEN": "test-token"}), \
                patch("scripts.github_sync.urlopen", return_value=io.BytesIO(b'{"full_name":"owner/repo"}')) as request:
            client = github_sync.GitHub("owner/repo")
            self.assertEqual(client.api("")["full_name"], "owner/repo")
        self.assertEqual(request.call_args.args[0].full_url, "https://api.github.com/repos/owner/repo")


class BundleValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        export_merge_records([record()], self.folder)

    def test_valid_bundle_roundtrips_cyrillic_and_stable_keys(self):
        manifest = github_sync.validate_bundle(self.folder)
        self.assertEqual(manifest["counts"]["profiles"], 1)
        self.assertEqual(github_sync.profile_ids((self.folder / "profiles.csv").read_text(encoding="utf-8")), {"123"})

    def test_changed_csv_is_rejected_before_publication(self):
        with (self.folder / "profiles.csv").open("a", encoding="utf-8") as stream:
            stream.write("broken")
        with self.assertRaisesRegex(ValueError, "hash/size"):
            github_sync.validate_bundle(self.folder)

    def test_empty_directory_is_never_publishable(self):
        export_merge_records([], self.folder)
        with self.assertRaisesRegex(ValueError, "empty"):
            github_sync.validate_bundle(self.folder)

    def test_orphan_contact_is_rejected_even_with_valid_hash(self):
        path = self.folder / "contacts.csv"
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            columns = reader.fieldnames
            rows = list(reader)
        self.assertTrue(rows)
        rows[0]["profile_id"] = "999"
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        manifest_path = self.folder / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"]["contacts"].update(sha256=github_sync.sha256(path), bytes=path.stat().st_size)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unknown parent"):
            github_sync.validate_bundle(self.folder)

    def test_contact_cannot_point_to_another_profiles_workplace(self):
        first = record()
        first["sections"]["about"] = {"practiceList": [{
            "institution": {"id": 87, "label": "МЦ Пример"},
            "location": {"address": "ул. Пример 7"},
            "contactList": {"email": "office@example.bg"},
        }]}
        export_merge_records([first, record(456)], self.folder)
        path = self.folder / "contacts.csv"
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            columns = reader.fieldnames
            rows = list(reader)
        linked = next(row for row in rows if row["workplace_key"])
        linked["profile_id"] = "456"
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        manifest_path = self.folder / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"]["contacts"].update(sha256=github_sync.sha256(path), bytes=path.stat().st_size)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "cross-profile"):
            github_sync.validate_bundle(self.folder)


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)

    def test_online_backup_includes_committed_wal_records(self):
        original = self.folder / "source.sqlite3"
        database = create_database(original)
        self.addCleanup(database.close)
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("INSERT INTO listings VALUES(456, '{}')")
        database.commit()
        archive = self.folder / "state.gz"
        github_sync.snapshot_database(original, archive)
        restored = self.folder / "restored.sqlite3"
        github_sync.restore_archive(archive, restored)
        with closing(sqlite3.connect(restored)) as connection:
            self.assertEqual(connection.execute("SELECT profile_id FROM listings ORDER BY profile_id").fetchall(), [(123,), (456,)])

    def test_corrupt_archive_preserves_existing_database(self):
        destination = self.folder / "destination.sqlite3"
        database = create_database(destination)
        database.close()
        before = destination.read_bytes()
        archive = self.folder / "corrupt.gz"
        archive.write_bytes(b"not gzip")
        with self.assertRaises(gzip.BadGzipFile):
            github_sync.restore_archive(archive, destination)
        self.assertEqual(destination.read_bytes(), before)
        self.assertFalse(destination.with_name(destination.name + ".restore").exists())

    def test_wrong_database_schema_is_rejected_without_replacement(self):
        source = self.folder / "wrong.sqlite3"
        with closing(sqlite3.connect(source)) as database:
            database.execute("CREATE TABLE unrelated(id INTEGER)")
        archive = self.folder / "wrong.gz"
        with gzip.open(archive, "wb") as stream:
            stream.write(source.read_bytes())
        destination = self.folder / "restored.sqlite3"
        with self.assertRaisesRegex(ValueError, "missing collector"):
            github_sync.restore_archive(archive, destination)
        self.assertFalse(destination.exists())

    def test_missing_checkpoint_allowed_only_without_previous_data(self):
        github = Mock(repository="owner/repo")
        github.release.return_value = None
        github.api.return_value = None
        destination = self.folder / "checkpoint.sqlite3"
        self.assertFalse(github_sync.restore_state(github, destination))
        github.api.return_value = {"name": "data"}
        with self.assertRaisesRegex(RuntimeError, "checkpoint release is missing"):
            github_sync.restore_state(github, destination)
        self.assertFalse(destination.exists())

    def test_existing_release_without_uploaded_asset_fails_closed(self):
        github = Mock(repository="owner/repo")
        github.release.return_value = {"id": 1, "assets": []}
        with self.assertRaisesRegex(RuntimeError, "empty restart"):
            github_sync.restore_state(github, self.folder / "checkpoint.sqlite3")

    def test_failed_download_does_not_start_empty_or_fall_back(self):
        github = Mock(repository="owner/repo")
        github.release.return_value = {"assets": [
            {"id": 2, "name": "checkpoint-new.sqlite3.gz", "state": "uploaded", "size": 12},
            {"id": 1, "name": "checkpoint-old.sqlite3.gz", "state": "uploaded", "size": 12},
        ]}
        with patch.object(github_sync, "run", side_effect=subprocess.CalledProcessError(1, "gh")) as command:
            with self.assertRaises(subprocess.CalledProcessError):
                github_sync.restore_state(github, self.folder / "checkpoint.sqlite3")
        self.assertEqual(command.call_count, 1)
        self.assertIn("checkpoint-new.sqlite3.gz", command.call_args.args)
        self.assertFalse((self.folder / "checkpoint.sqlite3").exists())

    def test_failed_upload_never_deletes_previous_checkpoint(self):
        original = self.folder / "source.sqlite3"
        create_database(original).close()
        github = Mock(repository="owner/repo")
        github.release.return_value = {"id": 1, "assets": [
            {"id": 1, "name": "checkpoint-old.sqlite3.gz", "state": "uploaded", "size": 12}]}
        with patch.object(github_sync, "run", side_effect=subprocess.CalledProcessError(1, "gh")):
            with self.assertRaises(subprocess.CalledProcessError):
                github_sync.save_state(github, original)
        github.api.assert_not_called()

    def test_regressed_database_is_rejected_before_checkpoint_upload(self):
        original = self.folder / "source.sqlite3"
        create_database(original).close()
        github = Mock(repository="owner/repo")
        with patch.object(github_sync, "remote_data_state", return_value=("old", {"123", "456"})):
            with patch.object(github_sync, "run") as command:
                with self.assertRaisesRegex(ValueError, "loses 1 existing"):
                    github_sync.save_state(github, original, self.folder)
        command.assert_not_called()
        github.release.assert_not_called()

    def test_csv_ids_must_match_checkpoint_before_publication(self):
        original = self.folder / "source.sqlite3"
        create_database(original).close()
        bundle = self.folder / "csv"
        export_merge_records([record(), record(456)], bundle)
        with self.assertRaisesRegex(ValueError, "do not match"):
            github_sync.validate_collection(bundle, original, self.folder)


class GitPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.remote = self.folder / "remote.git"
        self.source = self.folder / "source"
        self.source.mkdir()
        self.bundle = self.folder / "csv"
        github_sync.run("git", "init", "--bare", str(self.remote))
        github_sync.run("git", "init", str(self.source))
        github_sync.run("git", "config", "user.name", "Offline test", cwd=self.source)
        github_sync.run("git", "config", "user.email", "test@example.invalid", cwd=self.source)
        github_sync.run("git", "remote", "add", "origin", str(self.remote), cwd=self.source)
        (self.source / "source.txt").write_text("source file", encoding="utf-8")
        github_sync.run("git", "add", "source.txt", cwd=self.source)
        github_sync.run("git", "commit", "-m", "Source", cwd=self.source)
        self.source_head = github_sync.run("git", "rev-parse", "HEAD", cwd=self.source)

    def test_data_branch_contains_only_bundle_and_preserves_source_checkout(self):
        export_merge_records([record()], self.bundle)
        first = github_sync.publish_data(self.bundle, self.source)
        filenames = github_sync.run("git", "ls-tree", "--name-only", first, cwd=self.source).splitlines()
        self.assertEqual(set(filenames), set(github_sync.BUNDLE_FILES))
        self.assertEqual(github_sync.run("git", "rev-parse", "HEAD", cwd=self.source), self.source_head)
        self.assertEqual(github_sync.run("git", "status", "--porcelain", cwd=self.source), "")
        self.assertEqual(github_sync.publish_data(self.bundle, self.source), first)
        export_merge_records([record(), record(456)], self.bundle)
        second = github_sync.publish_data(self.bundle, self.source)
        self.assertNotEqual(first, second)
        self.assertEqual(github_sync.run("git", "rev-parse", second + "^", cwd=self.source), first)

    def test_losing_existing_profile_preserves_last_published_commit(self):
        export_merge_records([record(), record(456)], self.bundle)
        previous = github_sync.publish_data(self.bundle, self.source)
        export_merge_records([record(), record(789)], self.bundle)
        with self.assertRaisesRegex(ValueError, "loses 1 existing"):
            github_sync.publish_data(self.bundle, self.source)
        remote_commit = github_sync.run("git", "ls-remote", "origin", "refs/heads/data", cwd=self.source).split()[0]
        self.assertEqual(remote_commit, previous)


if __name__ == "__main__":
    unittest.main()
