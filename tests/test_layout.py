import base64
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_session_sync.config import Config
from claude_session_sync.layout import (
    DFRAME_STORE_KEY,
    GROUP_SCOPES_KEY,
    LOCAL_SLICE_KEY,
    LayoutError,
    LayoutBusyError,
    LayoutRecoveryError,
    LayoutSnapshot,
    LayoutSynchronizer,
    same_recovery_payload,
    transform_layout_records,
)
from claude_session_sync.model import Profile
import claude_session_sync.layout as layout_module


def encoded(document):
    return b"\x01" + json.dumps(document, separators=(",", ":")).encode("utf-8")


def decoded(value):
    return json.loads(value[1:].decode("utf-8"))


def scope(*groups, assignments=None):
    entries = [{"id": "id-{}".format(name.lower()), "name": name} for name in groups]
    return {
        "groups": entries,
        "assignments": assignments or {},
        "order": {
            entry["id"]: [
                session
                for session, group_id in (assignments or {}).items()
                if group_id == entry["id"]
            ]
            for entry in entries
        },
    }


def records(scopes, *, active=None, pins=None, extra=None):
    pins = list(pins or [])
    state = {
        "customGroupsByScope": copy.deepcopy(scopes),
        "pinnedOrder": pins,
        "homeProjectsPinnedOrder": [],
        "lastSidebarScopeKey": active,
        "unrelated": extra,
    }
    return {
        GROUP_SCOPES_KEY: encoded(
            {"value": copy.deepcopy(scopes), "timestamp": 1, "tabId": "tab"}
        ),
        LOCAL_SLICE_KEY: encoded(
            {
                "value": {
                    "pinnedOrder": pins,
                    "homeProjectsPinnedOrder": [],
                },
                "timestamp": 1,
                "tabId": "tab",
            }
        ),
        DFRAME_STORE_KEY: encoded({"state": state, "version": 4}),
    }


class LayoutTransformTests(unittest.TestCase):
    def test_recovery_comparison_preserves_unknown_json_types(self):
        before = encoded({"timestamp": 1, "value": {}, "unknown": True})
        current = encoded({"timestamp": 2, "value": {}, "unknown": 1})
        self.assertFalse(same_recovery_payload(GROUP_SCOPES_KEY, before, current))

    def test_recovery_comparison_rejects_malformed_or_unknown_metadata(self):
        before = encoded({"timestamp": 1, "value": {}})
        for timestamp in ("2", True, -1, None):
            with self.subTest(timestamp=timestamp):
                self.assertFalse(same_recovery_payload(
                    GROUP_SCOPES_KEY, before, encoded({"timestamp": timestamp, "value": {}})
                ))
        self.assertFalse(same_recovery_payload(GROUP_SCOPES_KEY, before, b"bad"))
        self.assertFalse(same_recovery_payload(GROUP_SCOPES_KEY, before, None))
        self.assertFalse(same_recovery_payload(b"unknown", before, encoded({"timestamp": 2, "value": {}})))

    def test_first_sync_unions_groups_and_populates_every_target_scope(self):
        first = scope(
            "Focus",
            assignments={"code:local_one": "id-focus"},
        )
        second = scope(
            "Backlog",
            assignments={"code:local_two": "id-backlog"},
        )
        current = records({"a/w": first, "b/w": second}, active="a/w")

        result = transform_layout_records(
            current,
            {
                "a/w": {"code:local_one", "code:local_two"},
                "b/w": {"code:local_one", "code:local_two"},
                "c/w": {"code:local_one", "code:local_two"},
            },
            timestamp_ms=10,
        )

        self.assertEqual(("Focus", "Backlog"), result.snapshot.groups)
        store = decoded(result.records[DFRAME_STORE_KEY])
        scopes = store["state"]["customGroupsByScope"]
        self.assertEqual({"a/w", "b/w", "c/w"}, set(scopes))
        for current_scope in scopes.values():
            self.assertEqual(
                ["Focus", "Backlog"],
                [group["name"] for group in current_scope["groups"]],
            )
            names = {group["id"]: group["name"] for group in current_scope["groups"]}
            assigned = {
                session: names[group_id]
                for session, group_id in current_scope["assignments"].items()
            }
            self.assertEqual(
                {
                    "code:local_one": "Focus",
                    "code:local_two": "Backlog",
                },
                assigned,
            )

    def test_ambiguous_assignment_is_not_copied_to_a_new_scope(self):
        first = scope("Focus", "Backlog", assignments={"code:local_one": "id-focus"})
        second = scope("Focus", "Backlog", assignments={"code:local_one": "id-backlog"})
        result = transform_layout_records(
            records({"a/w": first, "b/w": second}, active="a/w"),
            {
                "a/w": {"code:local_one"},
                "b/w": {"code:local_one"},
                "c/w": {"code:local_one"},
            },
            timestamp_ms=10,
        )

        self.assertEqual(1, result.ambiguous_assignments)
        scopes = decoded(result.records[DFRAME_STORE_KEY])["state"][
            "customGroupsByScope"
        ]
        self.assertTrue(scopes["a/w"]["assignments"])
        self.assertTrue(scopes["b/w"]["assignments"])
        self.assertEqual({}, scopes["c/w"]["assignments"])

    def test_snapshot_restores_groups_and_pins_after_an_empty_account_state(self):
        snapshot = LayoutSnapshot(
            groups=("Focus",),
            assignments={"code:local_one": "Focus"},
            pinned_order=("code:local_one",),
            home_projects_pinned_order=(),
        )
        result = transform_layout_records(
            records({}, active="new/w", pins=[]),
            {"new/w": {"code:local_one"}},
            snapshot=snapshot,
            timestamp_ms=10,
        )

        store = decoded(result.records[DFRAME_STORE_KEY])["state"]
        local = decoded(result.records[LOCAL_SLICE_KEY])["value"]
        self.assertEqual(["code:local_one"], store["pinnedOrder"])
        self.assertEqual(store["pinnedOrder"], local["pinnedOrder"])
        self.assertEqual(
            ["Focus"],
            [
                group["name"]
                for group in store["customGroupsByScope"]["new/w"]["groups"]
            ],
        )

    def test_snapshot_groups_survive_an_active_scope_update(self):
        snapshot = LayoutSnapshot(
            groups=("Focus", "Backlog"),
            assignments={},
            pinned_order=(),
            home_projects_pinned_order=(),
        )
        current = records(
            {
                "a/w": scope("Focus", "New"),
                "b/w": scope("Focus", "Backlog"),
            },
            active="a/w",
        )

        result = transform_layout_records(
            current,
            {"a/w": set(), "b/w": set()},
            snapshot=snapshot,
            timestamp_ms=10,
        )

        self.assertEqual(("Focus", "New", "Backlog"), result.snapshot.groups)
        repeated = transform_layout_records(
            result.records,
            {"a/w": set(), "b/w": set()},
            snapshot=result.snapshot,
            timestamp_ms=20,
        )
        self.assertEqual(result.records, repeated.records)

    def test_unrelated_store_fields_are_preserved(self):
        current = records(
            {"a/w": scope("Focus")}, active="a/w", extra={"keep": [1, 2, 3]}
        )
        result = transform_layout_records(current, {"a/w": set()}, timestamp_ms=10)

        self.assertEqual(
            {"keep": [1, 2, 3]},
            decoded(result.records[DFRAME_STORE_KEY])["state"]["unrelated"],
        )

    def test_one_sided_group_record_update_is_merged(self):
        current = records({"a/w": scope("Focus", "New")}, active="a/w")
        group_record = decoded(current[GROUP_SCOPES_KEY])
        group_record["value"] = {"a/w": scope("Focus")}
        current[GROUP_SCOPES_KEY] = encoded(group_record)

        result = transform_layout_records(current, {"a/w": set()}, timestamp_ms=10)

        store = decoded(result.records[DFRAME_STORE_KEY])["state"][
            "customGroupsByScope"
        ]
        persisted = decoded(result.records[GROUP_SCOPES_KEY])["value"]
        self.assertEqual(store, persisted)
        self.assertEqual(
            ["Focus", "New"],
            [group["name"] for group in store["a/w"]["groups"]],
        )

    def test_conflicting_group_ids_stop_without_a_replacement(self):
        current = records({"a/w": scope("Focus")}, active="a/w")
        group_record = decoded(current[GROUP_SCOPES_KEY])
        conflicting = scope("Focus")
        conflicting["groups"][0]["id"] = "different-id"
        conflicting["order"] = {"different-id": []}
        group_record["value"] = {"a/w": conflicting}
        current[GROUP_SCOPES_KEY] = encoded(group_record)

        with self.assertRaisesRegex(LayoutError, "conflicting ids"):
            transform_layout_records(current, {"a/w": set()}, timestamp_ms=10)


class LayoutRecoveryTests(unittest.TestCase):
    def config(self, root):
        data_root = root / "Claude"
        return Config(
            profiles=(Profile("Work", data_root, ("/usr/bin/true",), True),),
            state_dir=root / "state",
            retention=5,
            acknowledge_cross_profile_copy=False,
            acknowledge_cross_account_copy=True,
            claude_executable=Path("/usr/bin/true"),
            target_policy="all-configured-profiles",
            sync_sidebar_layout=True,
        )

    def test_prepared_journal_with_untouched_values_closes_as_rolled_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            journal_root = config.state_dir / "layout-runs"
            journal_root.mkdir(parents=True)
            database_path = config.profiles[0].data_root / "Local Storage" / "leveldb"
            database_path.mkdir(parents=True)
            before = b"before"
            journal_path = journal_root / "run.json"
            journal_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "run_id": "run",
                        "state": "PREPARED",
                        "database": str(database_path),
                        "records": [
                            {
                                "key": DFRAME_STORE_KEY.hex(),
                                "before": before.hex(),
                                "after_sha256": hashlib.sha256(b"after").hexdigest(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            class FakeDatabase:
                def __init__(self, helper, database):
                    pass

                def get(self, key):
                    return before

            synchronizer = LayoutSynchronizer(
                config, helper=root / "helper", process_probe=lambda: ()
            )
            with patch("claude_session_sync.layout.LevelDatabase", FakeDatabase):
                synchronizer._recover_pending()

            self.assertEqual(
                "ROLLED_BACK",
                json.loads(journal_path.read_text(encoding="utf-8"))["state"],
            )

    def test_legacy_mixed_recovery_preserves_independent_layout_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            database_path = config.profiles[0].data_root / "Local Storage" / "leveldb"
            database_path.mkdir(parents=True)
            journal_root = config.state_dir / "layout-runs"
            journal_root.mkdir(parents=True)
            journal_path = journal_root / "run.json"
            journal_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "state": "PREPARED",
                        "database": str(database_path),
                        "records": [
                            {
                                "key": key.hex(),
                                "before": b"before".hex(),
                                "after_sha256": hashlib.sha256(b"after").hexdigest(),
                            }
                            for key in (GROUP_SCOPES_KEY, DFRAME_STORE_KEY)
                        ],
                    }
                )
            )
            values = {GROUP_SCOPES_KEY: b"after", DFRAME_STORE_KEY: b"independent"}
            writes = []

            class FakeDatabase:
                def __init__(self, helper, database):
                    pass

                def get(self, key):
                    return values[key]

                def write(self, records, state):
                    writes.append(records)
                    values.update(records)

            synchronizer = LayoutSynchronizer(config, process_probe=lambda: ())
            with patch("claude_session_sync.layout.LevelDatabase", FakeDatabase):
                with self.assertRaisesRegex(LayoutRecoveryError, "independently"):
                    synchronizer._recover_pending()
            self.assertEqual([], writes)
            self.assertEqual(b"independent", values[DFRAME_STORE_KEY])
            self.assertEqual(
                "RECOVERY_REQUIRED", json.loads(journal_path.read_text())["state"]
            )

    def test_probe_opens_only_copied_database_and_leaves_live_files_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            database_path = config.profiles[0].data_root / "Local Storage" / "leveldb"
            database_path.mkdir(parents=True)
            marker = database_path / "CURRENT"
            marker.write_bytes(b"original")
            (config.profiles[0].data_root / "claude-code-sessions" / "a" / "w").mkdir(
                parents=True
            )
            helper = root / "helper"
            helper.write_bytes(b"placeholder")
            os.chmod(helper, 0o700)
            data = records({"a/w": scope("Focus")}, active="a/w")
            opened = []

            class FakeDatabase:
                def __init__(self, helper, database):
                    opened.append(database)
                    (database / "CURRENT").write_bytes(b"mutated on open")

                def get(self, key):
                    return data[key]

            synchronizer = LayoutSynchronizer(
                config, helper=helper, process_probe=lambda: ()
            )
            with patch("claude_session_sync.layout.LevelDatabase", FakeDatabase):
                result = synchronizer.probe()
            self.assertEqual("compatible", result["state"])
            self.assertEqual(1, result["group_count"])
            self.assertEqual(b"original", marker.read_bytes())
            self.assertTrue(opened)
            self.assertNotIn(database_path, opened)
            self.assertTrue(all(not path.exists() for path in opened))
            self.assertFalse(config.state_dir.exists())

    def test_running_claude_prevents_direct_sync_or_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            synchronizer = LayoutSynchronizer(
                self.config(Path(directory)), process_probe=lambda: (object(),)
            )
            with self.assertRaises(LayoutBusyError):
                synchronizer.sync()
            with self.assertRaises(LayoutBusyError):
                synchronizer.probe()

    def transaction_fixture(self, root):
        config = self.config(root)
        config.state_dir.mkdir()
        database_path = config.profiles[0].data_root / "Local Storage" / "leveldb"
        database_path.mkdir(parents=True)
        values = {DFRAME_STORE_KEY: b"before"}

        class FakeDatabase:
            def __init__(self, helper, database):
                self.database = database

            def get(self, key):
                return values[key]

            def write(self, records, state):
                values.update(records)

        synchronizer = LayoutSynchronizer(config, process_probe=lambda: ())
        return synchronizer, FakeDatabase, FakeDatabase(None, database_path), values

    def metadata_recovery_fixture(self, root, applied=False):
        synchronizer, fake, database, values = self.transaction_fixture(root)
        before = records(
            {"a/w": scope("Focus", assignments={"code:local_one": "id-focus"})},
            active="a/w",
        )
        store = decoded(before[DFRAME_STORE_KEY])
        store["state"]["collapsedGroups"] = []
        before[DFRAME_STORE_KEY] = encoded(store)
        targets = {"a/w": {"code:local_one"}, "b/w": {"code:local_one"}}
        transformed = transform_layout_records(before, targets, timestamp_ms=10)
        after = dict(transformed.records)
        values.clear()
        values.update(after if applied else before)
        for key in (GROUP_SCOPES_KEY, LOCAL_SLICE_KEY):
            document = decoded(values[key])
            document["timestamp"] = 20
            values[key] = encoded(document)
        store = decoded(values[DFRAME_STORE_KEY])
        store["state"]["collapsedGroups"] = ["Focus"]
        values[DFRAME_STORE_KEY] = encoded(store)
        journal = synchronizer._journal()
        journal.root.mkdir()
        snapshot = synchronizer.config.state_dir / "sidebar-layout-0.json"
        snapshot.write_text(json.dumps(transformed.snapshot.as_dict()))
        items = [
            (synchronizer._record_target(database.database, key), before[key], after[key])
            for key in before
        ]
        items.append((synchronizer._snapshot_target(snapshot), snapshot.read_bytes(), snapshot.read_bytes()))
        path = journal.root / "interrupted.json"
        path.write_text(json.dumps({
            "version": 2, "state": "RECOVERY_REQUIRED",
            "records": [{
                "target": target,
                "before": base64.b64encode(old).decode(),
                "after": base64.b64encode(new).decode(),
                "before_sha256": hashlib.sha256(old).hexdigest(),
                "after_sha256": hashlib.sha256(new).hexdigest(),
            } for target, old, new in items],
        }))
        return synchronizer, fake, values, path, targets

    def test_metadata_only_relaunch_recovers_and_next_sync_restores_groups(self):
        for applied in (False, True):
            with self.subTest(applied=applied), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                synchronizer, fake, values, path, targets = self.metadata_recovery_fixture(root, applied)
                current = values.copy()
                original_records = json.loads(path.read_text())["records"]
                with patch("claude_session_sync.layout.LevelDatabase", fake):
                    current_hashes = {
                        item["target"]: hashlib.sha256(
                            synchronizer._journal().read(item["target"])
                        ).hexdigest()
                        for item in original_records
                    }
                    synchronizer._recover_pending()
                self.assertEqual(current, values)
                document = json.loads(path.read_text())
                self.assertEqual("COMMITTED" if applied else "ROLLED_BACK", document["state"])
                self.assertEqual("adapter-payload", document["recovery_comparison"])
                self.assertEqual(original_records, document["records"])
                self.assertEqual(current_hashes, document["recovery_observed_sha256"])
                synchronizer.helper = root / "helper"
                synchronizer.helper.write_bytes(b"fixture")
                os.chmod(synchronizer.helper, 0o700)
                with patch("claude_session_sync.layout.LevelDatabase", fake), patch.object(
                    synchronizer, "_target_sessions", return_value=targets
                ):
                    first = synchronizer.sync()
                    second = synchronizer.sync()
                self.assertEqual(1, first.group_count)
                self.assertEqual("noop", second.state)
                store = decoded(values[DFRAME_STORE_KEY])["state"]
                self.assertEqual(["Focus"], store["collapsedGroups"])
                for target in targets:
                    self.assertEqual("Focus", store["customGroupsByScope"][target]["groups"][0]["name"])

    def test_metadata_recovery_does_not_hide_real_or_unknown_edits(self):
        for field, value in (("pinnedOrder", ["code:other"]), ("unrelated", "new"),
                             ("customGroupsByScope", {}), ("collapsedGroups", [3])):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                synchronizer, fake, values, path, _targets = self.metadata_recovery_fixture(Path(directory))
                document = decoded(values[DFRAME_STORE_KEY])
                document["state"][field] = value
                values[DFRAME_STORE_KEY] = encoded(document)
                current = values.copy()
                with patch("claude_session_sync.layout.LevelDatabase", fake):
                    with self.assertRaises(LayoutRecoveryError):
                        synchronizer._recover_pending()
                self.assertEqual(current, values)
                self.assertEqual("RECOVERY_REQUIRED", json.loads(path.read_text())["state"])

    def test_metadata_recovery_keeps_mixed_payloads_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            synchronizer, fake, values, path, _targets = self.metadata_recovery_fixture(Path(directory))
            document = json.loads(path.read_text())
            group_after = base64.b64decode(document["records"][0]["after"])
            values[GROUP_SCOPES_KEY] = group_after
            current = values.copy()
            with patch("claude_session_sync.layout.LevelDatabase", fake):
                with self.assertRaises(LayoutRecoveryError):
                    synchronizer._recover_pending()
            self.assertEqual(current, values)

    def test_snapshot_and_layout_share_one_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            synchronizer, fake, database, values = self.transaction_fixture(
                Path(directory)
            )
            snapshot = synchronizer.config.state_dir / "sidebar-layout-0.json"
            with patch("claude_session_sync.layout.LevelDatabase", fake):
                synchronizer._commit(
                    database,
                    values.copy(),
                    {DFRAME_STORE_KEY: b"after"},
                    snapshot_path=snapshot,
                    snapshot_after=b"new snapshot",
                )
            document = json.loads(
                next(synchronizer._journal_root().glob("*.json")).read_text()
            )
            self.assertEqual("COMMITTED", document["state"])
            self.assertEqual(2, len(document["records"]))
            self.assertEqual(b"new snapshot", snapshot.read_bytes())
            self.assertEqual(b"after", values[DFRAME_STORE_KEY])

    def test_snapshot_write_failure_rolls_back_layout_too(self):
        with tempfile.TemporaryDirectory() as directory:
            synchronizer, fake, database, values = self.transaction_fixture(
                Path(directory)
            )
            snapshot = synchronizer.config.state_dir / "sidebar-layout-0.json"
            original_write = layout_module.atomic_write_bytes

            def fail_snapshot(path, content):
                if path == snapshot:
                    raise OSError("snapshot disk full")
                original_write(path, content)

            with (
                patch("claude_session_sync.layout.LevelDatabase", fake),
                patch(
                    "claude_session_sync.layout.atomic_write_bytes",
                    side_effect=fail_snapshot,
                ),
            ):
                with self.assertRaisesRegex(LayoutError, "rolled back"):
                    synchronizer._commit(
                        database,
                        values.copy(),
                        {DFRAME_STORE_KEY: b"after"},
                        snapshot_path=snapshot,
                        snapshot_after=b"new snapshot",
                    )
            self.assertEqual(b"before", values[DFRAME_STORE_KEY])
            self.assertFalse(snapshot.exists())

    def test_interrupted_snapshot_commit_recovers_before_and_after_together(self):
        with tempfile.TemporaryDirectory() as directory:
            synchronizer, fake, database, values = self.transaction_fixture(
                Path(directory)
            )
            snapshot = synchronizer.config.state_dir / "sidebar-layout-0.json"
            journal = synchronizer._journal()
            original_save = journal._save

            def interrupt_commit(path, document):
                if document["state"] == "COMMITTED":
                    raise KeyboardInterrupt()
                original_save(path, document)

            with (
                patch("claude_session_sync.layout.LevelDatabase", fake),
                patch.object(synchronizer, "_journal", return_value=journal),
                patch.object(journal, "_save", side_effect=interrupt_commit),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    synchronizer._commit(
                        database,
                        values.copy(),
                        {DFRAME_STORE_KEY: b"after"},
                        snapshot_path=snapshot,
                        snapshot_after=b"new snapshot",
                    )
            with patch("claude_session_sync.layout.LevelDatabase", fake):
                synchronizer._recover_pending()
            document = json.loads(
                next(synchronizer._journal_root().glob("*.json")).read_text()
            )
            self.assertEqual("COMMITTED", document["state"])
            self.assertEqual(b"new snapshot", snapshot.read_bytes())
            self.assertEqual(b"after", values[DFRAME_STORE_KEY])

    def test_fifo_snapshot_fails_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            os.mkfifo(path)
            code = (
                "from pathlib import Path; from claude_session_sync.layout import load_snapshot, LayoutError; "
                "\ntry: load_snapshot(Path({!r}))\nexcept LayoutError: pass\nelse: raise AssertionError('accepted FIFO')"
            ).format(str(path))
            result = subprocess.run(
                [sys.executable, "-c", code], capture_output=True, timeout=3
            )
            self.assertEqual(0, result.returncode, result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
