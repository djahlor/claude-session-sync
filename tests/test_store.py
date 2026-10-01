import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_session_sync.config import Config
from claude_session_sync.hash_cache import HashCache
from claude_session_sync.model import Profile
from claude_session_sync.store import SessionStore


class StoreTests(unittest.TestCase):
    def config(self, root: Path) -> Config:
        return Config(
            profiles=(
                Profile("Claude Personal", root / "personal", ("open", "Personal")),
                Profile("Claude", root / "standard", ("open", "Claude")),
            ),
            state_dir=root / "state",
            retention=10,
            acknowledge_cross_profile_copy=True,
            acknowledge_cross_account_copy=True,
            claude_executable=Path("/Applications/Claude.app/Contents/MacOS/Claude"),
        )

    def write_session(
        self,
        data_root: Path,
        account: str,
        workspace: str,
        session_id: str,
        extra: object = None,
    ) -> Path:
        directory = data_root / "claude-code-sessions" / account / workspace
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "local_{}.json".format(session_id)
        payload = {"sessionId": "local_" + session_id}
        if isinstance(extra, dict):
            payload.update(extra)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_discovers_standard_and_personal_roots_in_stable_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            self.write_session(root / "personal", "z-account", "b-workspace", "third")
            self.write_session(root / "standard", "a-account", "z-workspace", "second")
            self.write_session(root / "standard", "a-account", "a-workspace", "first")

            discovery = SessionStore().discover(config)

            self.assertEqual(
                [
                    ("Claude", "a-account", "a-workspace", "first"),
                    ("Claude", "a-account", "z-workspace", "second"),
                    ("Claude Personal", "z-account", "b-workspace", "third"),
                ],
                [
                    (
                        replica.target.profile_name,
                        replica.target.account_id,
                        replica.target.workspace_id,
                        replica.session_id,
                    )
                    for replica in discovery.replicas
                ],
            )

    def test_unreadable_records_are_left_alone_and_symlinks_block(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            target = (
                root / "standard" / "claude-code-sessions" / "account" / "workspace"
            )
            target.mkdir(parents=True)
            (
                root / "personal" / "claude-code-sessions" / "account" / "workspace"
            ).mkdir(parents=True)
            (target / "local_bad-json.json").write_text("{", encoding="utf-8")
            (target / "local_array.json").write_text("[]", encoding="utf-8")
            (target / "local_wrong.json").write_text(
                json.dumps({"sessionId": 7}), encoding="utf-8"
            )
            valid = self.write_session(
                root / "standard",
                "account",
                "workspace",
                "valid",
                {"sessionSettings": {}, "enabledMcpTools": ["example"]},
            )
            (target / "local_link.json").symlink_to(valid)

            discovery = SessionStore().discover(config)

            # A record Claude would not accept freezes only its own chat.
            readable = {
                replica.session_id: replica.state_hash is not None
                for replica in discovery.replicas
            }
            self.assertEqual(
                {"array": False, "bad-json": False, "valid": True, "wrong": False},
                readable,
            )
            self.assertEqual(
                ["local_link.json"],
                [invalid.path.name for invalid in discovery.invalid_replicas],
            )
            self.assertIn("symlink", discovery.invalid_replicas[0].reason)

    def test_a_record_naming_another_session_is_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            path = self.write_session(
                root / "standard",
                "account",
                "workspace",
                "registry-id",
            )
            path.write_text(
                json.dumps({"sessionId": "different-upstream-session-id"}),
                encoding="utf-8",
            )
            (
                root / "personal" / "claude-code-sessions" / "account" / "workspace"
            ).mkdir(parents=True)

            discovery = SessionStore().discover(config)

            self.assertEqual(
                ["registry-id"], [item.session_id for item in discovery.replicas]
            )
            self.assertIsNone(discovery.replicas[0].state_hash)
            self.assertEqual((), discovery.invalid_replicas)


class HashCacheTests(unittest.TestCase):
    def test_cache_state_is_owner_only_before_any_transaction_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "new-state" / "hash-cache.sqlite3"

            cache = HashCache(cache_path)
            cache.close()

            self.assertEqual(0o700, stat.S_IMODE(cache_path.parent.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(cache_path.stat().st_mode))

    def test_warm_discovery_does_not_read_validated_replica_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store_tests = StoreTests()
            config = store_tests.config(root)
            written = store_tests.write_session(
                root / "standard", "account", "workspace", "cached"
            )
            # A file saved in the last two seconds is always read again.
            os.utime(written, ns=(1_000_000_000, 1_000_000_000))
            cache = HashCache(root / "state" / "hashes.sqlite3")
            store = SessionStore(cache)
            first = store.discover(config)

            with patch.object(
                Path, "read_bytes", side_effect=AssertionError("cache miss")
            ):
                second = store.discover(config)
            cache.close()

            self.assertEqual(first.replicas, second.replicas)

    def test_malformed_replica_never_enters_validated_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store_tests = StoreTests()
            config = store_tests.config(root)
            target = (
                root / "standard" / "claude-code-sessions" / "account" / "workspace"
            )
            target.mkdir(parents=True)
            malformed = target / "local_bad.json"
            malformed.write_text("{", encoding="utf-8")
            cache = HashCache(root / "state" / "hashes.sqlite3")
            store = SessionStore(cache)
            store.discover(config)

            original_read_bytes = Path.read_bytes
            reads = []

            def tracked_read_bytes(path: Path) -> bytes:
                reads.append(path)
                return original_read_bytes(path)

            with patch.object(
                Path, "read_bytes", autospec=True, side_effect=tracked_read_bytes
            ):
                store.discover(config)
            cache.close()

            self.assertEqual([malformed], reads)

    def test_a_same_size_rewrite_that_keeps_the_mtime_is_read_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store_tests = StoreTests()
            config = store_tests.config(root)
            written = store_tests.write_session(
                root / "standard", "account", "workspace", "cached", {"title": "aaaa"}
            )
            os.utime(written, ns=(1_000_000_000, 1_000_000_000))
            cache = HashCache(root / "state" / "hashes.sqlite3")
            store = SessionStore(cache)
            first = store.discover(config).replicas[0]

            store_tests.write_session(
                root / "standard", "account", "workspace", "cached", {"title": "bbbb"}
            )
            os.utime(written, ns=(1_000_000_000, 1_000_000_000))
            second = store.discover(config).replicas[0]
            cache.close()

            self.assertEqual(first.size, second.size)
            self.assertNotEqual(first.state_hash, second.state_hash)

    def test_a_cache_from_an_older_version_is_rebuilt_and_never_trusted(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store_tests = StoreTests()
            config = store_tests.config(root)
            written = store_tests.write_session(
                root / "standard", "account", "workspace", "cached", {"title": "real"}
            )
            os.utime(written, ns=(1_000_000_000, 1_000_000_000))
            cache_path = root / "state" / "hashes.sqlite3"
            cache_path.parent.mkdir(mode=0o700)
            metadata = written.stat()
            old = sqlite3.connect(str(cache_path))
            old.execute(
                "CREATE TABLE file_hashes (path TEXT NOT NULL, size INTEGER NOT NULL, "
                "mtime_ns INTEGER NOT NULL, ctime_ns INTEGER NOT NULL, inode INTEGER NOT NULL, "
                "session_id TEXT, digest TEXT NOT NULL, state_hash TEXT, activity INTEGER, "
                "normalisation TEXT, PRIMARY KEY (path, size, mtime_ns, ctime_ns, inode))"
            )
            from claude_session_sync.fingerprint import normalisation

            old.execute(
                "INSERT INTO file_hashes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(written), metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns,
                 metadata.st_ino, "cached", "0" * 64, "stale", 7, normalisation()),
            )
            old.execute("PRAGMA user_version = 1")
            old.commit()
            old.close()

            cache = HashCache(cache_path)
            replica = SessionStore(cache).discover(config).replicas[0]
            cache.close()

            self.assertNotEqual("0" * 64, replica.digest)
            self.assertNotEqual("stale", replica.state_hash)


if __name__ == "__main__":
    unittest.main()
