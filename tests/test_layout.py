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
    LayoutTransform,
    DFRAME_STORE_KEY,
    GROUP_SCOPES_KEY,
    LOCAL_SLICE_KEY,
    LayoutError,
    LayoutBusyError,
    LayoutChoiceError,
    LayoutDisagreementError,
    LayoutReceipt,
    LayoutRecoveryError,
    LayoutSnapshot,
    LayoutSynchronizer,
    _decode_record,
    _encode_record,
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


def recovery_record(target, before, after):
    """One version 2 recovery record, as the record journal writes it."""

    return {
        "target": target,
        "before": base64.b64encode(before).decode("ascii"),
        "after": base64.b64encode(after).decode("ascii"),
        "before_sha256": hashlib.sha256(before).hexdigest(),
        "after_sha256": hashlib.sha256(after).hexdigest(),
    }


def edited(current, scope_key, new_scope):
    """A sidebar edit as Claude saves it: in both group records."""
    changed = dict(current)
    store = decoded(changed[DFRAME_STORE_KEY])
    store["state"]["customGroupsByScope"][scope_key] = new_scope
    changed[DFRAME_STORE_KEY] = encoded(store)
    persisted = decoded(changed[GROUP_SCOPES_KEY])
    persisted["value"][scope_key] = new_scope
    changed[GROUP_SCOPES_KEY] = encoded(persisted)
    return changed


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
    def test_chromium_string_encodings_round_trip_emoji_and_latin_one(self):
        document = {"groups": ["Focus 🚀", "Café"]}
        utf16 = b"\x00" + json.dumps(
            document, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-16-le")
        latin1 = b"\x01" + json.dumps(
            {"group": "Café"}, ensure_ascii=False, separators=(",", ":")
        ).encode("latin-1")

        self.assertEqual(document, _decode_record(utf16, "utf16"))
        self.assertEqual({"group": "Café"}, _decode_record(latin1, "latin1"))
        encoded_document = _encode_record(document)
        self.assertEqual(b"\x01", encoded_document[:1])
        self.assertTrue(all(byte < 128 for byte in encoded_document[1:]))
        self.assertEqual(document, _decode_record(encoded_document, "round trip"))

    def test_chromium_string_decoder_rejects_malformed_values(self):
        malformed = (
            b"",
            b"\x02{}",
            b"\x00{",
            b"\x00" + b"{\x00",
            b"\x01not-json",
        )
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(LayoutError):
                _decode_record(value, "record")

    def test_adopt_current_sidebar_preserves_source_and_seeds_future_scopes(self):
        source = scope(
            "Focus 🚀",
            "Backlog",
            assignments={
                "code:keep": "id-focus 🚀",
                "code:moved": "id-backlog",
                "code:dangling": "id-focus 🚀",
            },
        )
        source["groups"][0]["color"] = "violet"
        source["scopeStatus"] = {"attention": "keep"}
        source["order"] = {
            "id-focus 🚀": ["code:keep", "code:moved", "code:dangling"],
            "id-backlog": ["code:moved"],
        }
        old_target = scope("Old")
        old_target["targetStatus"] = "keep"
        historical = scope("Stale")
        current = records(
            {"a/w": source, "b/w": old_target, "history/w": historical},
            active="a/w",
            pins=["code:keep"],
            extra={"attention": "preserve"},
        )
        targets = {
            "a/w": {"code:keep", "code:moved"},
            "b/w": {"code:keep", "code:moved"},
        }

        adopted = transform_layout_records(
            current,
            targets,
            adopt_current_sidebar=True,
            timestamp_ms=10,
        )
        adopted_state = decoded(adopted.records[DFRAME_STORE_KEY])["state"]
        self.assertEqual(source, adopted_state["customGroupsByScope"]["a/w"])
        self.assertEqual(historical, adopted_state["customGroupsByScope"]["history/w"])
        self.assertEqual({"attention": "preserve"}, adopted_state["unrelated"])
        copied = adopted_state["customGroupsByScope"]["b/w"]
        self.assertEqual("keep", copied["targetStatus"])
        self.assertEqual(source["groups"], copied["groups"])
        self.assertEqual(source["assignments"], copied["assignments"])
        self.assertEqual(
            ["code:keep", "code:dangling"], copied["order"]["id-focus 🚀"]
        )
        self.assertEqual(LayoutSnapshot("a/w"), adopted.snapshot)

        next_targets = dict(targets, **{"c/w": {"code:keep", "code:moved"}})
        repeated = transform_layout_records(
            adopted.records,
            next_targets,
            snapshot=adopted.snapshot,
            timestamp_ms=20,
        )
        repeated_state = decoded(repeated.records[DFRAME_STORE_KEY])["state"]
        self.assertEqual(source, repeated_state["customGroupsByScope"]["a/w"])
        new_scope = repeated_state["customGroupsByScope"]["c/w"]
        self.assertEqual(["Focus 🚀", "Backlog"], [g["name"] for g in new_scope["groups"]])
        self.assertEqual("violet", new_scope["groups"][0]["color"])
        # After adoption the signed-in account's filing is copied whole, so a
        # chat that has not reached this account yet is already filed when it does.
        self.assertEqual(
            ["code:keep", "code:dangling"], new_scope["order"]["id-focus 🚀"]
        )
        self.assertEqual(["code:moved"], new_scope["order"]["id-backlog"])

    def test_adopted_snapshot_fails_closed_on_a_divergent_account_scope(self):
        source = scope(
            "Focus", assignments={"code:keep": "id-focus"}
        )
        adopted = transform_layout_records(
            records(
                {"a/w": source, "b/w": scope("Old")},
                active="a/w",
            ),
            {"a/w": {"code:keep"}, "b/w": {"code:keep"}},
            adopt_current_sidebar=True,
            timestamp_ms=10,
        )
        switched = copy.deepcopy(decoded(adopted.records[DFRAME_STORE_KEY]))
        switched["state"]["lastSidebarScopeKey"] = "b/w"
        switched["state"]["customGroupsByScope"]["b/w"] = scope("Old")
        current = dict(adopted.records)
        current[DFRAME_STORE_KEY] = _encode_record(switched)

        with self.assertRaisesRegex(LayoutChoiceError, "keep-sidebar --account N --apply"):
            transform_layout_records(
                current,
                {"a/w": {"code:keep"}, "b/w": {"code:keep"}},
                snapshot=adopted.snapshot,
                timestamp_ms=20,
            )

    def test_adopted_snapshot_bootstraps_an_explicitly_empty_active_scope(self):
        source = scope("Focus", assignments={"code:keep": "id-focus"})
        source["groups"][0]["color"] = "violet"
        adopted = transform_layout_records(
            records({"a/w": source}, active="a/w"),
            {"a/w": {"code:keep"}},
            adopt_current_sidebar=True,
            timestamp_ms=10,
        )
        current = {}
        for key, value in adopted.records.items():
            document = _decode_record(value, "record")
            if key == DFRAME_STORE_KEY:
                document["state"]["lastSidebarScopeKey"] = "c/w"
                document["state"]["customGroupsByScope"]["c/w"] = scope()
            elif key == GROUP_SCOPES_KEY:
                document["value"]["c/w"] = scope()
            current[key] = _encode_record(document)

        restored = transform_layout_records(
            current,
            {"a/w": {"code:keep"}, "c/w": {"code:keep"}},
            snapshot=adopted.snapshot,
            timestamp_ms=20,
        )
        active = decoded(restored.records[DFRAME_STORE_KEY])["state"][
            "customGroupsByScope"
        ]["c/w"]
        self.assertEqual(source["groups"], active["groups"])
        self.assertEqual(source["assignments"], active["assignments"])

    def test_explicit_source_scope_restores_the_hydrated_active_target(self):
        source = scope(
            "Focus", assignments={"code:keep": "id-focus"}
        )
        source["groups"][0]["icon"] = "star"
        target = scope("Old", assignments={"code:keep": "id-old"})
        current = records({"source/w": source, "target/w": target}, active="target/w")

        restored = transform_layout_records(
            current,
            {"source/w": {"code:keep"}, "target/w": {"code:keep"}},
            adopt_source_scope="source/w",
            timestamp_ms=10,
        )
        state = decoded(restored.records[DFRAME_STORE_KEY])["state"]
        self.assertEqual(source, state["customGroupsByScope"]["source/w"])
        self.assertEqual(source, state["customGroupsByScope"]["target/w"])
        self.assertEqual("target/w", restored.canonical_upload_scope)

    def test_adopting_the_current_sidebar_rejects_missing_empty_or_unapproved_scope(self):
        for active, scopes in (
            (None, {"a/w": scope("Focus")}),
            ("a/w", {}),
            ("a/w", {"a/w": scope()}),
            ("other/w", {"other/w": scope("Focus")}),
        ):
            with self.subTest(active=active, scopes=scopes):
                with self.assertRaisesRegex(LayoutError, "approved sync target"):
                    transform_layout_records(
                        records(scopes, active=active), {"a/w": set()},
                        adopt_current_sidebar=True,
                    )

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

    def test_recovery_comparison_accepts_only_consumed_scoped_migration_markers(self):
        marker = layout_module.GROUP_UPLOAD_KEY
        self.assertTrue(same_recovery_payload(marker, b"\x01a/w|migrate", None))
        for expected in (
            b"\x01a/w", b"\x01a/w|unknown", b"a/w|migrate", b"\x01|migrate",
            b"\x01/w|migrate", b"\x01a/|migrate", b"\x01a/w/extra|migrate",
            b"\x01a/w|migrate|migrate", b"\x01a/ w|migrate", b"\x01a/\x00w|migrate",
            b"\x01a/\xff|migrate",
        ):
            with self.subTest(expected=expected):
                self.assertFalse(same_recovery_payload(marker, expected, None))
        self.assertFalse(same_recovery_payload(marker, b"\x01a/w|migrate", b"\x01other/w|migrate"))
        self.assertFalse(same_recovery_payload(marker, None, b"\x01a/w|migrate"))


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
            synchronizer = LayoutSynchronizer(
                config, helper=root / "helper", process_probe=lambda: ()
            )
            journal_path = journal_root / "run.json"
            journal_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "state": "PREPARED",
                        "records": [
                            recovery_record(
                                synchronizer._record_target(database_path, DFRAME_STORE_KEY),
                                before,
                                b"after",
                            )
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

            with patch("claude_session_sync.layout.LevelDatabase", FakeDatabase):
                synchronizer._recover_pending()

            self.assertEqual(
                "ROLLED_BACK",
                json.loads(journal_path.read_text(encoding="utf-8"))["state"],
            )

    def test_mixed_recovery_preserves_independent_layout_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            database_path = config.profiles[0].data_root / "Local Storage" / "leveldb"
            database_path.mkdir(parents=True)
            journal_root = config.state_dir / "layout-runs"
            journal_root.mkdir(parents=True)
            synchronizer = LayoutSynchronizer(config, process_probe=lambda: ())
            journal_path = journal_root / "run.json"
            journal_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "state": "PREPARED",
                        "records": [
                            recovery_record(
                                synchronizer._record_target(database_path, key), b"before", b"after"
                            )
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

                def get_optional(self, key):
                    return data.get(key)

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

            def get_optional(self, key):
                return values.get(key)

            def write(self, records, state):
                for key, value in records.items():
                    if value is None:
                        values.pop(key, None)
                    else:
                        values[key] = value

        synchronizer = LayoutSynchronizer(config, process_probe=lambda: ())
        return synchronizer, FakeDatabase, FakeDatabase(None, database_path), values

    def test_group_upload_marker_checks_identity_and_preserves_pending_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            synchronizer, _fake, database, values = self.transaction_fixture(Path(directory))
            before = records({"a/w": scope("Focus")}, active="a/w")
            after = records({"a/w": scope("Focus", "Backlog")}, active="a/w")
            baseline = {
                layout_module.SYNC_OWNER_KEY: b"\x01a",
                layout_module.SYNC_ACTIVE_KEY: b"\x011",
            }
            values.clear()
            values.update(baseline)
            self.assertEqual(
                (None, b"\x01a/w"),
                synchronizer._group_upload_marker(database, before, after, canonical_upload_scope="a/w"),
            )
            self.assertIsNone(synchronizer._group_upload_marker(database, after, after, canonical_upload_scope="a/w"))
            self.assertIsNone(synchronizer._group_upload_marker(database, before, after))
            values[layout_module.GROUP_UPLOAD_KEY] = b"\x01a/w|migrate"
            with self.assertRaisesRegex(LayoutBusyError, "another account sidebar update is pending"):
                synchronizer._group_upload_marker(database, before, after, canonical_upload_scope="a/w")
            for changed in (
                {layout_module.GROUP_UPLOAD_KEY: b"\x01a/w"},
                {layout_module.SYNC_OWNER_KEY: b"\x01other"},
                {layout_module.SYNC_OWNER_KEY: None},
                {layout_module.SYNC_ACTIVE_KEY: b"\x01unknown"},
                {layout_module.SYNC_QUARANTINE_KEY: b"\x011"},
                {layout_module.GROUP_UPLOAD_KEY: b"\x01other/w|migrate"},
                {layout_module.GROUP_UPLOAD_KEY: b"\x011"},
            ):
                with self.subTest(changed=changed):
                    values.clear()
                    values.update(baseline)
                    values.update(changed)
                    with self.assertRaises(LayoutError):
                        synchronizer._group_upload_marker(database, before, after, canonical_upload_scope="a/w")
            for active in (None, b"\x010"):
                values.clear()
                values[layout_module.SYNC_ACTIVE_KEY] = active
                self.assertIsNone(synchronizer._group_upload_marker(database, before, after, canonical_upload_scope="a/w"))

    def test_pending_group_delete_or_rename_defers_records_and_snapshot(self):
        renamed = scope("Focus", "Backlog")
        renamed["groups"][1]["name"] = "Renamed"
        for edited in (scope(), scope("Focus"), renamed):
            with self.subTest(edited=edited), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                synchronizer, fake, _database, values = self.transaction_fixture(root)
                values.clear()
                values.update(records({"a/w": edited, "b/w": scope("Focus", "Backlog")}, active="a/w"))
                values.update({
                    layout_module.SYNC_OWNER_KEY: b"\x01a",
                    layout_module.SYNC_ACTIVE_KEY: b"\x011",
                    layout_module.GROUP_UPLOAD_KEY: b"\x01a/w",
                })
                snapshot_path = synchronizer.config.state_dir / "sidebar-layout-0.json"
                snapshot_path.write_text(json.dumps(LayoutSnapshot("b/w").as_dict()))
                snapshot_before = snapshot_path.read_bytes()
                records_before = values.copy()
                synchronizer.helper = root / "helper"
                synchronizer.helper.write_bytes(b"fixture")
                os.chmod(synchronizer.helper, 0o700)
                with patch("claude_session_sync.layout.LevelDatabase", fake), patch.object(
                    synchronizer, "_target_sessions", return_value={"a/w": set(), "b/w": set()}
                ), patch.object(fake, "write", side_effect=AssertionError("must not write")):
                    with self.assertRaisesRegex(LayoutError, "user edit is pending"):
                        synchronizer.sync(after_account_switch=True)
                self.assertEqual(records_before, values)
                self.assertEqual(snapshot_before, snapshot_path.read_bytes())
                self.assertFalse(synchronizer._journal_root().exists())

    def test_inactive_scope_changes_still_validate_account_metadata(self):
        for changed_record in (GROUP_SCOPES_KEY, DFRAME_STORE_KEY):
            with self.subTest(changed_record=changed_record), tempfile.TemporaryDirectory() as directory:
                synchronizer, _fake, database, values = self.transaction_fixture(Path(directory))
                before = records({"a/w": scope("Focus"), "b/w": scope()}, active="a/w")
                after = dict(before)
                updated = records({"a/w": scope("Focus"), "b/w": scope("Focus")}, active="a/w")
                after[changed_record] = updated[changed_record]
                baseline = {
                    layout_module.SYNC_OWNER_KEY: b"\x01a",
                    layout_module.SYNC_ACTIVE_KEY: b"\x011",
                }
                for changed in (
                    {layout_module.GROUP_UPLOAD_KEY: b"\x01b/w"},
                    {layout_module.GROUP_UPLOAD_KEY: b"\x01b/w|migrate"},
                    {layout_module.GROUP_UPLOAD_KEY: b"\x01unknown"},
                    {layout_module.SYNC_QUARANTINE_KEY: b"\x011"},
                    {layout_module.SYNC_OWNER_KEY: b"\x01b"},
                    {layout_module.SYNC_OWNER_KEY: None},
                ):
                    with self.subTest(changed=changed):
                        values.clear()
                        values.update(baseline)
                        values.update(changed)
                        with self.assertRaises(LayoutError):
                            synchronizer._group_upload_marker(database, before, after)
                for pending in (None, b"\x01a/w", b"\x01a/w|migrate"):
                    values.clear()
                    values.update(baseline)
                    if pending is not None:
                        values[layout_module.GROUP_UPLOAD_KEY] = pending
                    self.assertIsNone(synchronizer._group_upload_marker(database, before, after))
                    self.assertEqual(pending, values.get(layout_module.GROUP_UPLOAD_KEY))

    def test_inactive_scope_metadata_failure_preserves_records_and_snapshot(self):
        for changed in (
            {layout_module.GROUP_UPLOAD_KEY: b"\x01b/w"},
            {layout_module.SYNC_QUARANTINE_KEY: b"\x011"},
            {layout_module.SYNC_OWNER_KEY: None},
        ):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                synchronizer, fake, _database, values = self.transaction_fixture(root)
                values.clear()
                values.update(records({"a/w": scope("Focus"), "b/w": scope()}, active="a/w"))
                values.update({layout_module.SYNC_OWNER_KEY: b"\x01a", layout_module.SYNC_ACTIVE_KEY: b"\x011"})
                values.update(changed)
                snapshot = synchronizer.config.state_dir / "sidebar-layout-0.json"
                snapshot.write_text(json.dumps(LayoutSnapshot("a/w").as_dict()))
                snapshot_before = snapshot.read_bytes()
                records_before = values.copy()
                synchronizer.helper = root / "helper"
                synchronizer.helper.write_bytes(b"fixture")
                os.chmod(synchronizer.helper, 0o700)
                with patch("claude_session_sync.layout.LevelDatabase", fake), patch.object(
                    synchronizer, "_target_sessions", return_value={"a/w": set(), "b/w": set()}
                ), patch.object(fake, "write") as write:
                    with self.assertRaises(LayoutError):
                        synchronizer.sync()
                    write.assert_not_called()
                self.assertEqual(records_before, values)
                self.assertEqual(snapshot_before, snapshot.read_bytes())
                self.assertFalse(synchronizer._journal_root().exists())

    def test_group_upload_marker_is_removed_when_the_layout_write_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            synchronizer, fake, database, values = self.transaction_fixture(Path(directory))
            marker = layout_module.GROUP_UPLOAD_KEY
            original = dict(values)
            original_write = fake.write

            def fail_store_write(self, replacements, state):
                if replacements.get(DFRAME_STORE_KEY) == b"after":
                    raise LayoutError("injected layout write failure")
                original_write(self, replacements, state)

            with patch("claude_session_sync.layout.LevelDatabase", fake), patch.object(fake, "write", fail_store_write):
                with self.assertRaises(LayoutError):
                    synchronizer._commit(
                        database,
                        {marker: None, DFRAME_STORE_KEY: b"before"},
                        {marker: b"\x01a/w|migrate", DFRAME_STORE_KEY: b"after"},
                    )
            self.assertEqual(original, values)
            journal = next(synchronizer._journal_root().glob("*.json"))
            self.assertEqual("ROLLED_BACK", json.loads(journal.read_text())["state"])

    def metadata_recovery_fixture(self, root, applied=False, marker=None, relaunch=True):
        synchronizer, fake, database, values = self.transaction_fixture(root)
        before = records(
            {"a/w": scope("Focus", assignments={"code:local_one": "id-focus"})},
            active="a/w",
        )
        store = decoded(before[DFRAME_STORE_KEY])
        store["state"]["collapsedGroups"] = []
        before[DFRAME_STORE_KEY] = encoded(store)
        targets = {"a/w": {"code:local_one"}, "b/w": {"code:local_one"}}
        transformed = transform_layout_records(
            before, targets, timestamp_ms=10, adopt_current_sidebar=True,
        )
        after = dict(transformed.records)
        values.clear()
        values.update(after if applied else before)
        if relaunch:
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
        # An older install had not adopted an account yet.
        old_snapshot = json.dumps({"version": 1, "adopted_scope": None}).encode()
        new_snapshot = json.dumps(transformed.snapshot.as_dict()).encode()
        if marker is None:
            old_snapshot = new_snapshot
        snapshot.write_bytes(new_snapshot if applied else old_snapshot)
        items = [
            (synchronizer._record_target(database.database, key), before[key], after[key])
            for key in before
        ]
        items.append((synchronizer._snapshot_target(snapshot), old_snapshot, new_snapshot))
        if marker is not None:
            items.append((synchronizer._record_target(database.database, layout_module.GROUP_UPLOAD_KEY), None, marker))
        path = journal.root / "interrupted.json"
        path.write_text(json.dumps({
            "version": 2, "state": "PREPARED" if marker is not None else "RECOVERY_REQUIRED",
            "records": [{
                "target": target,
                "before": None if old is None else base64.b64encode(old).decode(),
                "after": base64.b64encode(new).decode(),
                "before_sha256": None if old is None else hashlib.sha256(old).hexdigest(),
                "after_sha256": hashlib.sha256(new).hexdigest(),
            } for target, old, new in items],
        }))
        return synchronizer, fake, values, path, targets

    def test_consumed_migration_marker_recovers_only_whole_transaction_phases_without_writes(self):
        for applied in (False, True):
            for relaunch in (False, True):
                with self.subTest(applied=applied, relaunch=relaunch), tempfile.TemporaryDirectory() as directory:
                    synchronizer, fake, values, path, _targets = self.metadata_recovery_fixture(
                        Path(directory), applied, marker=b"\x01a/w|migrate", relaunch=relaunch,
                    )
                    snapshot = synchronizer.config.state_dir / "sidebar-layout-0.json"
                    current = values.copy()
                    snapshot_before = snapshot.read_bytes()
                    with patch("claude_session_sync.layout.LevelDatabase", fake), patch.object(
                        fake, "write", side_effect=AssertionError("must not rewrite layout")
                    ) as write, patch.object(layout_module, "atomic_write_bytes") as write_snapshot:
                        synchronizer._recover_pending()
                        write.assert_not_called()
                        write_snapshot.assert_not_called()
                    self.assertEqual("COMMITTED" if applied else "ROLLED_BACK", json.loads(path.read_text())["state"])
                    self.assertEqual(current, values)
                    self.assertEqual(snapshot_before, snapshot.read_bytes())

    def test_consumed_marker_does_not_hide_user_edits_unknown_markers_or_mixed_payloads(self):
        cases = ("user-edit", "unknown-marker", "unknown-data", "mixed-layout", "mixed-snapshot")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                marker = {"user-edit": b"\x01a/w", "unknown-marker": b"\x01a/w|unknown"}.get(case, b"\x01a/w|migrate")
                synchronizer, fake, values, path, _targets = self.metadata_recovery_fixture(
                    Path(directory), True, marker=marker,
                )
                document = json.loads(path.read_text())
                snapshot = synchronizer.config.state_dir / "sidebar-layout-0.json"
                if case == "unknown-data":
                    store = decoded(values[DFRAME_STORE_KEY])
                    store["state"]["unknown"] = "independent edit"
                    values[DFRAME_STORE_KEY] = encoded(store)
                elif case == "mixed-layout":
                    values[GROUP_SCOPES_KEY] = base64.b64decode(document["records"][0]["before"])
                elif case == "mixed-snapshot":
                    snapshot.write_bytes(base64.b64decode(document["records"][-2]["before"]))
                current = values.copy()
                snapshot_before = snapshot.read_bytes()
                with patch("claude_session_sync.layout.LevelDatabase", fake), patch.object(
                    fake, "write", side_effect=AssertionError("must not rewrite layout")
                ) as write, patch.object(layout_module, "atomic_write_bytes") as write_snapshot:
                    with self.assertRaises(LayoutRecoveryError):
                        synchronizer._recover_pending()
                    write.assert_not_called()
                    write_snapshot.assert_not_called()
                self.assertEqual("RECOVERY_REQUIRED", json.loads(path.read_text())["state"])
                self.assertEqual(current, values)
                self.assertEqual(snapshot_before, snapshot.read_bytes())

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


class FollowAccountTests(unittest.TestCase):
    """After adoption, one account's sidebar is the source of truth at a time."""

    config = LayoutRecoveryTests.config
    transaction_fixture = LayoutRecoveryTests.transaction_fixture
    targets_ab = {"a/w": {"code:x"}, "b/w": {"code:x"}}

    def adopted(self, source, other, targets):
        return transform_layout_records(
            records({"a/w": source, "b/w": other}, active="a/w"),
            targets,
            adopt_current_sidebar=True,
            timestamp_ms=10,
        )

    def switched_to_b(self, adopted, b_scope):
        document = copy.deepcopy(decoded(adopted.records[DFRAME_STORE_KEY]))
        document["state"]["lastSidebarScopeKey"] = "b/w"
        document["state"]["customGroupsByScope"]["b/w"] = b_scope
        current = dict(adopted.records)
        current[DFRAME_STORE_KEY] = _encode_record(document)
        return current

    def test_after_a_switch_the_account_just_left_is_copied_into_the_new_one(self):
        targets = {"a/w": {"code:x", "code:y"}, "b/w": {"code:x", "code:y"}}
        latest = scope("Focus", "Admin", assignments={"code:x": "id-focus", "code:y": "id-admin"})
        adopted = self.adopted(latest, scope("Old"), targets)
        # Claude reloaded b's groups from its servers at sign-in: an old list.
        current = self.switched_to_b(adopted, scope("Old", "Stale"))

        result = transform_layout_records(
            current, targets, snapshot=adopted.snapshot, timestamp_ms=20,
            after_account_switch=True, owner_account="b",
        )

        scopes = decoded(result.records[DFRAME_STORE_KEY])["state"]["customGroupsByScope"]
        self.assertEqual(latest["groups"], scopes["b/w"]["groups"])
        self.assertEqual(latest["assignments"], scopes["b/w"]["assignments"])
        self.assertEqual("b/w", result.canonical_upload_scope, "b's servers must get the copy")
        self.assertEqual("b/w", result.snapshot.adopted_scope)

    def test_without_a_switch_the_signed_in_account_is_copied_and_a_deleted_group_stays_gone(self):
        targets = {"a/w": {"code:x"}, "b/w": {"code:x"}}
        adopted = self.adopted(
            scope("Focus", "Old idea", assignments={"code:x": "id-focus"}), scope(), targets
        )
        current = edited(adopted.records, "a/w", scope("Focus", assignments={"code:x": "id-focus"}))

        result = transform_layout_records(
            current, targets, snapshot=adopted.snapshot, timestamp_ms=20, owner_account="a"
        )

        scopes = decoded(result.records[DFRAME_STORE_KEY])["state"]["customGroupsByScope"]
        for key in targets:
            self.assertEqual(["Focus"], [group["name"] for group in scopes[key]["groups"]])
        self.assertIsNone(result.canonical_upload_scope)

    def test_a_chat_moved_in_the_signed_in_account_moves_in_every_account(self):
        targets = {key: {"code:x"} for key in ("a/w", "b/w", "c/w")}
        adopted = self.adopted(
            scope("Focus", "Backlog", assignments={"code:x": "id-focus"}), scope(), targets
        )
        current = edited(adopted.records, "a/w", scope("Focus", "Backlog", assignments={"code:x": "id-backlog"}))

        result = transform_layout_records(
            current, targets, snapshot=adopted.snapshot, timestamp_ms=20, owner_account="a"
        )

        scopes = decoded(result.records[DFRAME_STORE_KEY])["state"]["customGroupsByScope"]
        for key in targets:
            self.assertEqual({"code:x": "id-backlog"}, scopes[key]["assignments"])
            self.assertEqual({"id-focus": [], "id-backlog": ["code:x"]}, scopes[key]["order"])
        repeated = transform_layout_records(
            result.records, targets, snapshot=result.snapshot, timestamp_ms=30, owner_account="a"
        )
        self.assertEqual(result.records, repeated.records)

    def test_an_account_whose_sidebar_claude_has_not_shown_yet_is_found_by_sign_in(self):
        targets = {"a/w": {"code:x"}, "b/w": {"code:x"}}
        latest = scope("Focus", assignments={"code:x": "id-focus"})
        adopted = self.adopted(latest, scope(), targets)

        result = transform_layout_records(
            adopted.records, targets, snapshot=adopted.snapshot, timestamp_ms=20,
            after_account_switch=True, owner_account="b",
        )

        state = decoded(result.records[DFRAME_STORE_KEY])["state"]
        self.assertEqual("b/w", state["lastSidebarScopeKey"])
        self.assertEqual(latest["groups"], state["customGroupsByScope"]["b/w"]["groups"])
        self.assertEqual("b/w", result.canonical_upload_scope)

    def test_an_account_that_does_not_sync_yet_leaves_every_record_alone(self):
        targets = {"a/w": {"code:x"}, "b/w": {"code:x"}}
        adopted = self.adopted(scope("Focus"), scope(), targets)

        result = transform_layout_records(
            adopted.records, targets, snapshot=adopted.snapshot, timestamp_ms=20,
            after_account_switch=True, owner_account="new",
        )

        self.assertEqual(dict(adopted.records), dict(result.records))
        self.assertIsNone(result.snapshot, "the snapshot file stays as it is")

    def test_deleting_the_last_group_in_the_main_account_changes_nothing_and_says_so(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            synchronizer, fake, _database, values = self.transaction_fixture(root)
            adopted = self.adopted(scope("Focus"), scope(), self.targets_ab)
            values.clear()
            values.update(adopted.records)
            document = copy.deepcopy(decoded(values[DFRAME_STORE_KEY]))
            document["state"]["customGroupsByScope"]["a/w"] = scope()
            values[DFRAME_STORE_KEY] = _encode_record(document)
            snapshot_path = synchronizer.config.state_dir / "sidebar-layout-0.json"
            snapshot_path.write_text(json.dumps(adopted.snapshot.as_dict()))
            before = dict(values)
            synchronizer.helper = root / "helper"
            synchronizer.helper.write_bytes(b"fixture")
            os.chmod(synchronizer.helper, 0o700)

            with patch("claude_session_sync.layout.LevelDatabase", fake), patch.object(
                synchronizer, "_target_sessions", return_value=self.targets_ab
            ):
                receipt = synchronizer.sync()

            self.assertEqual(LayoutReceipt("noop", 1, 0, 0, 0, 0, "main-account-has-no-groups"), receipt)
            self.assertEqual(before, values)
            self.assertEqual({"version": 1, "adopted_scope": "a/w"}, json.loads(snapshot_path.read_text()))

    def test_a_profile_left_alone_on_purpose_keeps_its_reason_when_another_changed(self):
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            synchronizer, fake, _database, values = self.transaction_fixture(root)
            second = Profile("Second", root / "second", ("open",), False)
            (second.data_root / "Local Storage" / "leveldb").mkdir(parents=True)
            synchronizer.config = replace(
                synchronizer.config, profiles=synchronizer.config.profiles + (second,)
            )
            synchronizer.helper = root / "helper"
            synchronizer.helper.write_bytes(b"fixture")
            os.chmod(synchronizer.helper, 0o700)
            plans = iter([
                ({DFRAME_STORE_KEY: b"before"}, {DFRAME_STORE_KEY: b"after"},
                 LayoutTransform({}, None, 1, 0, 0)),
                ({DFRAME_STORE_KEY: b"before"}, {DFRAME_STORE_KEY: b"before"},
                 LayoutTransform({}, None, 0, 0, 0, reason="main-account-has-no-groups")),
            ])

            with patch("claude_session_sync.layout.LevelDatabase", fake), patch.object(
                synchronizer, "_target_sessions", return_value=self.targets_ab
            ), patch.object(
                synchronizer, "_plan_profile", side_effect=lambda *args, **kwargs: next(plans)
            ), patch.object(synchronizer, "_commit"):
                receipt = synchronizer.sync()

            self.assertEqual("synced", receipt.state)
            self.assertEqual("main-account-has-no-groups", receipt.reason)

    def test_a_divergent_account_without_a_switch_is_left_for_a_choice(self):
        targets = {"a/w": {"code:x"}, "b/w": {"code:x"}}
        adopted = self.adopted(scope("Focus"), scope(), targets)
        current = self.switched_to_b(adopted, scope("Edited in b"))

        with self.assertRaisesRegex(LayoutChoiceError, "keep-sidebar --account N --apply"):
            transform_layout_records(
                current, targets, snapshot=adopted.snapshot, timestamp_ms=20, owner_account="b"
            )

    def test_a_switch_sync_writes_the_copy_and_asks_claude_to_upload_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            synchronizer, fake, _database, values = self.transaction_fixture(root)
            targets = {"a/w": {"code:x"}, "b/w": {"code:x"}}
            latest = scope("Focus", assignments={"code:x": "id-focus"})
            adopted = self.adopted(latest, scope(), targets)
            values.clear()
            values.update(self.switched_to_b(adopted, scope("Old")))
            prefix = layout_module.ORIGIN_PREFIX
            values[prefix + b"ccd-sync-owner"] = b"\x01b"
            values[prefix + b"ccd-sync-active"] = b"\x011"
            snapshot_path = synchronizer.config.state_dir / "sidebar-layout-0.json"
            snapshot_path.write_text(json.dumps(adopted.snapshot.as_dict()))
            synchronizer.helper = root / "helper"
            synchronizer.helper.write_bytes(b"fixture")
            os.chmod(synchronizer.helper, 0o700)
            with patch("claude_session_sync.layout.LevelDatabase", fake), patch.object(
                synchronizer, "_target_sessions", return_value=targets
            ):
                receipt = synchronizer.sync(after_account_switch=True)
                repeated = synchronizer.sync()

            self.assertEqual("synced", receipt.state)
            self.assertEqual("noop", repeated.state)
            scopes = decoded(values[DFRAME_STORE_KEY])["state"]["customGroupsByScope"]
            self.assertEqual(latest["groups"], scopes["b/w"]["groups"])
            self.assertEqual(b"\x01b/w", values[layout_module.GROUP_UPLOAD_KEY])
            self.assertEqual("b/w", json.loads(snapshot_path.read_text())["adopted_scope"])

    def test_a_pending_adoption_is_applied_once_when_claude_is_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            synchronizer, fake, _database, values = self.transaction_fixture(root)
            targets = {"a/w": {"code:x"}, "b/w": {"code:x"}}
            latest = scope("Focus", assignments={"code:x": "id-focus"})
            values.clear()
            # b is signed in; a holds the organization to keep.
            values.update(records({"a/w": latest, "b/w": scope("Old")}, active="b/w"))
            prefix = layout_module.ORIGIN_PREFIX
            values[prefix + b"ccd-sync-owner"] = b"\x01b"
            values[prefix + b"ccd-sync-active"] = b"\x011"
            layout_module.request_adoption(synchronizer.config.state_dir, "a/w")
            synchronizer.helper = root / "helper"
            synchronizer.helper.write_bytes(b"fixture")
            os.chmod(synchronizer.helper, 0o700)
            with patch("claude_session_sync.layout.LevelDatabase", fake), patch.object(
                synchronizer, "_target_sessions", return_value=targets
            ):
                receipt = synchronizer.sync()

            self.assertEqual("synced", receipt.state)
            scopes = decoded(values[DFRAME_STORE_KEY])["state"]["customGroupsByScope"]
            self.assertEqual(latest["groups"], scopes["b/w"]["groups"])
            self.assertIsNone(layout_module.read_pending_adoption(synchronizer.config.state_dir))
            self.assertEqual(b"\x01b/w", values[layout_module.GROUP_UPLOAD_KEY])


def group_names(records_by_key, keys):
    scopes = decoded(records_by_key[DFRAME_STORE_KEY])["state"]["customGroupsByScope"]
    return {key: [group["name"] for group in scopes[key]["groups"]] for key in keys}


class FirstAdoptionTests(unittest.TestCase):
    """With no adopted account yet, one account's sidebar becomes the source."""

    targets = {"a/w": {"code:x"}, "b/w": {"code:x"}}

    def first_sync(self, scopes, active):
        return transform_layout_records(
            records(scopes, active=active), self.targets, timestamp_ms=10
        )

    def test_accounts_holding_the_same_groups_adopt_the_signed_in_one(self):
        result = self.first_sync(
            {
                "a/w": scope("Focus", "Admin", assignments={"code:x": "id-admin"}),
                "b/w": scope("Admin", "Focus", assignments={"code:x": "id-focus"}),
            },
            active="a/w",
        )

        scopes = decoded(result.records[DFRAME_STORE_KEY])["state"]["customGroupsByScope"]
        self.assertEqual({"code:x": "id-admin"}, scopes["b/w"]["assignments"])
        self.assertEqual({"a/w": ["Focus", "Admin"], "b/w": ["Focus", "Admin"]}, group_names(result.records, self.targets))
        self.assertEqual(LayoutSnapshot("a/w"), result.snapshot)
        self.assertIsNone(result.canonical_upload_scope, "a's own groups need no upload")

    def test_different_groups_stop_for_a_choice_even_when_the_signed_in_account_has_groups(self):
        current = records({"a/w": scope("Focus"), "b/w": scope("Other")}, active="a/w")

        with self.assertRaisesRegex(LayoutChoiceError, "keep-sidebar --account N --apply"):
            transform_layout_records(current, self.targets, timestamp_ms=10)

    def test_an_empty_signed_in_account_gets_the_only_account_with_groups(self):
        result = self.first_sync({"a/w": scope(), "b/w": scope("Focus")}, active="a/w")

        self.assertEqual({"a/w": ["Focus"], "b/w": ["Focus"]}, group_names(result.records, self.targets))
        self.assertEqual(
            LayoutSnapshot("a/w", {"groups": ["Focus"], "assignments": {}, "order": {"Focus": []}}),
            result.snapshot,
            "a counts as the source only once Claude uploads the copy",
        )
        self.assertEqual("a/w", result.canonical_upload_scope, "a's servers must get the copy")

    def test_no_groups_anywhere_changes_nothing_and_says_so(self):
        current = records({"a/w": scope(), "b/w": scope()}, active="a/w")

        result = transform_layout_records(current, self.targets, timestamp_ms=10)

        self.assertEqual(current, dict(result.records))
        self.assertEqual("no-groups-yet", result.reason)
        self.assertIsNone(result.snapshot, "nothing is adopted yet")

    def test_an_empty_signed_in_account_and_two_grouped_accounts_stop_for_a_choice(self):
        targets = dict(self.targets, **{"c/w": {"code:x"}})
        with self.assertRaisesRegex(LayoutChoiceError, "keep-sidebar --account N --apply"):
            transform_layout_records(
                records({"a/w": scope(), "b/w": scope("Focus"), "c/w": scope("Other")}, active="a/w"),
                targets,
                timestamp_ms=10,
            )

    def test_a_signed_in_account_outside_sync_leaves_every_record_alone(self):
        current = records({"a/w": scope("Focus"), "stale/w": scope("Stale")}, active="stale/w")

        result = transform_layout_records(current, self.targets, timestamp_ms=10)

        self.assertEqual(current, dict(result.records))
        self.assertIsNone(result.reason)


class FirstSyncTests(unittest.TestCase):
    """The first closed sync on a fresh or upgraded install, through the synchronizer."""

    config = LayoutRecoveryTests.config
    transaction_fixture = LayoutRecoveryTests.transaction_fixture
    targets = {"a/w": {"code:x"}, "b/w": {"code:x"}}

    def installed(self, root, scopes, *, active, owner=None):
        synchronizer, fake, _database, values = self.transaction_fixture(root)
        values.clear()
        values.update(records(scopes, active=active))
        if owner is not None:
            values[layout_module.SYNC_OWNER_KEY] = b"\x01" + owner.encode("utf-8")
            values[layout_module.SYNC_ACTIVE_KEY] = b"\x011"
        synchronizer.helper = root / "helper"
        synchronizer.helper.write_bytes(b"fixture")
        os.chmod(synchronizer.helper, 0o700)

        def sync(**options):
            with patch("claude_session_sync.layout.LevelDatabase", fake), patch.object(
                synchronizer, "_target_sessions", return_value=self.targets
            ):
                return synchronizer.sync(**options)

        return synchronizer, values, sync

    def edit(self, values, scope_key, new_scope):
        """Claude writes a sidebar edit to both group records."""
        store = decoded(values[DFRAME_STORE_KEY])
        store["state"]["customGroupsByScope"][scope_key] = new_scope
        values[DFRAME_STORE_KEY] = encoded(store)
        persisted = decoded(values[GROUP_SCOPES_KEY])
        persisted["value"][scope_key] = new_scope
        values[GROUP_SCOPES_KEY] = encoded(persisted)

    def test_a_group_deleted_in_one_account_stays_deleted_after_the_next_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            both = scope("Focus", "Old idea", assignments={"code:x": "id-focus"})
            _synchronizer, values, sync = self.installed(
                Path(directory), {"a/w": both, "b/w": both}, active="a/w"
            )

            sync()
            self.edit(values, "a/w", scope("Focus", assignments={"code:x": "id-focus"}))
            sync()
            repeated = sync()

            self.assertEqual({"a/w": ["Focus"], "b/w": ["Focus"]}, group_names(values, self.targets))
            self.assertEqual("noop", repeated.state)

    def test_an_install_from_the_union_model_adopts_at_its_next_closed_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            union = scope("Focus", "Backlog", assignments={"code:x": "id-focus"})
            synchronizer, values, sync = self.installed(
                Path(directory), {"a/w": union, "b/w": union}, active="b/w", owner="b"
            )
            snapshot_path = synchronizer.config.state_dir / "sidebar-layout-0.json"
            # The shape the union model wrote before this change.
            snapshot_path.write_text(json.dumps({
                "version": 1,
                "groups": ["Focus", "Backlog"],
                "assignments": {"code:x": "Focus"},
                "pinned_order": ["code:x"],
                "home_projects_pinned_order": [],
                "group_order": {"Backlog": [], "Focus": ["code:x"]},
                "group_records": [{"id": "id-focus", "name": "Focus"}, {"id": "id-backlog", "name": "Backlog"}],
                "adopted_scope": None,
            }, indent=2, sort_keys=True) + "\n")

            sync()
            self.assertEqual({"version": 1, "adopted_scope": "b/w"}, json.loads(snapshot_path.read_text()))
            self.edit(values, "b/w", scope("Focus", assignments={"code:x": "id-focus"}))
            sync()

            self.assertEqual({"a/w": ["Focus"], "b/w": ["Focus"]}, group_names(values, self.targets))

    def test_a_choice_made_at_install_is_copied_into_the_signed_in_account(self):
        with tempfile.TemporaryDirectory() as directory:
            latest = scope("Focus", assignments={"code:x": "id-focus"})
            # b is signed in, but Claude has not shown b's Code sidebar yet.
            synchronizer, values, sync = self.installed(
                Path(directory), {"a/w": latest, "b/w": scope("Old")}, active="a/w", owner="b"
            )
            layout_module.request_adoption(synchronizer.config.state_dir, "a/w")

            receipt = sync()

            self.assertEqual("synced", receipt.state)
            self.assertEqual({"a/w": ["Focus"], "b/w": ["Focus"]}, group_names(values, self.targets))
            self.assertEqual(b"\x01b/w", values[layout_module.GROUP_UPLOAD_KEY])
            snapshot = json.loads((synchronizer.config.state_dir / "sidebar-layout-0.json").read_text())
            self.assertEqual("b/w", snapshot["adopted_scope"])
            self.assertIsNone(layout_module.read_pending_adoption(synchronizer.config.state_dir))

    def test_no_groups_anywhere_is_reported_and_nothing_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            synchronizer, values, sync = self.installed(
                Path(directory), {"a/w": scope(), "b/w": scope()}, active="a/w"
            )
            before = dict(values)

            receipt = sync()

            self.assertEqual(LayoutReceipt("noop", 1, 0, 0, 0, 0, "no-groups-yet"), receipt)
            self.assertEqual(before, values)
            self.assertFalse((synchronizer.config.state_dir / "sidebar-layout-0.json").exists())


class DoctorTests(unittest.TestCase):
    """doctor plans the next closed sync with the inputs sync uses."""

    config = LayoutRecoveryTests.config
    transaction_fixture = LayoutRecoveryTests.transaction_fixture
    targets = {"a/w": {"code:x"}, "b/w": {"code:x"}}

    def switched_to_b(self, root):
        """a was synced last; b is signed in and Claude reloaded b's old groups."""
        synchronizer, fake, _database, values = self.transaction_fixture(root)
        values.clear()
        values.update(records(
            {"a/w": scope("Focus", assignments={"code:x": "id-focus"}), "b/w": scope("Old")},
            active="b/w",
        ))
        values[layout_module.SYNC_OWNER_KEY] = b"\x01b"
        values[layout_module.SYNC_ACTIVE_KEY] = b"\x011"
        # The snapshot the adoption code wrote before this change.
        (synchronizer.config.state_dir / "sidebar-layout-0.json").write_text(json.dumps({
            "version": 1,
            "groups": ["Focus"],
            "assignments": {"code:x": "Focus"},
            "pinned_order": [],
            "home_projects_pinned_order": [],
            "group_order": {"Focus": ["code:x"]},
            "group_records": [{"id": "id-focus", "name": "Focus"}],
            "adopted_scope": "a/w",
        }))
        synchronizer.helper = root / "helper"
        synchronizer.helper.write_bytes(b"fixture")
        os.chmod(synchronizer.helper, 0o700)
        patches = (
            patch("claude_session_sync.layout.LevelDatabase", fake),
            patch.object(synchronizer, "_target_sessions", return_value=self.targets),
        )
        return synchronizer, values, patches

    def test_after_a_switch_doctor_accepts_the_account_the_user_chose(self):
        with tempfile.TemporaryDirectory() as directory:
            synchronizer, values, (database, targets) = self.switched_to_b(Path(directory))
            layout_module.request_adoption(synchronizer.config.state_dir, "a/w")

            with database, targets:
                probed = synchronizer.probe()
                synced = synchronizer.sync()

            self.assertEqual(
                {"state": "compatible", "profile_count": 1, "group_count": 1,
                 "pin_count": 0, "assignment_count": 1},
                probed,
            )
            self.assertEqual(("synced", 1), (synced.state, synced.group_count))
            self.assertEqual({"a/w": ["Focus"], "b/w": ["Focus"]}, group_names(values, self.targets))

    def test_after_a_switch_without_a_choice_doctor_never_advises_the_reloaded_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            synchronizer, values, (database, targets) = self.switched_to_b(Path(directory))

            with database, targets:
                with self.assertRaises(LayoutChoiceError) as raised:
                    synchronizer.probe()
                with self.assertRaises(LayoutChoiceError):
                    synchronizer.sync()

            self.assertIn("keep-sidebar --dry-run", str(raised.exception))
            self.assertNotIn("adopt-current-sidebar", str(raised.exception))

    def test_each_stop_is_reported_with_its_own_reason(self):
        from claude_session_sync.adapters import run_adapters

        for error, reason in (
            (LayoutChoiceError("which one?"), "choose-main-account"),
            (LayoutDisagreementError("which one?"), "sidebar-records-disagree"),
            (LayoutError("which one?"), "unsafe-layout"),
        ):
            class Stopped:
                def probe(self, error=error):
                    raise error

            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                config = self.config(Path(directory))
                dependencies = type("Dependencies", (), {"layout_factory": lambda self, _config: Stopped()})()

                result = run_adapters(config, dependencies, lambda: False, probe_only=True)

                self.assertEqual(
                    {"layout": {"state": "skipped", "reason": reason, "detail": "which one?"}},
                    result,
                )


class DisagreeingCopiesTests(unittest.TestCase):
    """Claude saves groups and pins twice; a copy never drops what only one save holds."""

    config = LayoutRecoveryTests.config
    transaction_fixture = LayoutRecoveryTests.transaction_fixture
    targets = {"a/w": {"code:x"}, "b/w": {"code:x"}}

    def test_a_group_or_pin_in_only_one_save_stops_the_copy(self):
        current = records({"a/w": scope("Focus"), "b/w": scope()}, active="a/w", pins=["code:x"])
        group_only_persisted = dict(current)
        persisted = decoded(current[GROUP_SCOPES_KEY])
        persisted["value"]["a/w"] = scope("Focus", "Only here")
        group_only_persisted[GROUP_SCOPES_KEY] = encoded(persisted)
        pin_only_local = dict(current)
        local = decoded(current[LOCAL_SLICE_KEY])
        local["value"]["pinnedOrder"] = ["code:x", "code:y"]
        pin_only_local[LOCAL_SLICE_KEY] = encoded(local)

        for label, disagreeing in (("groups", group_only_persisted), ("pins", pin_only_local)):
            with self.subTest(label), self.assertRaisesRegex(
                LayoutDisagreementError, "two saved copies of the .*{}".format(label)
            ):
                transform_layout_records(disagreeing, self.targets, timestamp_ms=10)

    def test_a_disagreement_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            synchronizer, fake, _database, values = self.transaction_fixture(root)
            values.clear()
            values.update(records({"a/w": scope("Focus"), "b/w": scope()}, active="a/w"))
            persisted = decoded(values[GROUP_SCOPES_KEY])
            persisted["value"]["a/w"] = scope("Focus", "Only here")
            values[GROUP_SCOPES_KEY] = encoded(persisted)
            before = dict(values)
            synchronizer.helper = root / "helper"
            synchronizer.helper.write_bytes(b"fixture")
            os.chmod(synchronizer.helper, 0o700)

            with patch("claude_session_sync.layout.LevelDatabase", fake), patch.object(
                synchronizer, "_target_sessions", return_value=self.targets
            ), self.assertRaises(LayoutDisagreementError):
                synchronizer.sync()

            self.assertEqual(before, values)
            self.assertFalse((synchronizer.config.state_dir / "sidebar-layout-0.json").exists())


class UnconfirmedUploadTests(unittest.TestCase):
    """A copied layout counts only after Claude uploads it as the account's own."""

    config = LayoutRecoveryTests.config
    transaction_fixture = LayoutRecoveryTests.transaction_fixture
    targets = {"a/w": {"code:x"}, "b/w": {"code:x"}}
    latest = scope("Focus", assignments={"code:x": "id-focus"})

    def switched_and_relaunched(self, root, *, b_after_relaunch, upload_consumed):
        """a was the source; the switch to b copied a into b and asked Claude to upload it."""
        synchronizer, fake, _database, values = self.transaction_fixture(root)
        values.clear()
        values.update(records({"a/w": self.latest, "b/w": scope("Old")}, active="b/w"))
        values[layout_module.SYNC_OWNER_KEY] = b"\x01b"
        values[layout_module.SYNC_ACTIVE_KEY] = b"\x011"
        (synchronizer.config.state_dir / "sidebar-layout-0.json").write_text(
            json.dumps({"version": 1, "adopted_scope": "a/w"})
        )
        synchronizer.helper = root / "helper"
        synchronizer.helper.write_bytes(b"fixture")
        os.chmod(synchronizer.helper, 0o700)
        database = patch("claude_session_sync.layout.LevelDatabase", fake)
        targets = patch.object(synchronizer, "_target_sessions", return_value=self.targets)
        with database, targets:
            synchronizer.sync(after_account_switch=True)
        self.assertEqual(b"\x01b/w", values[layout_module.GROUP_UPLOAD_KEY])
        # Claude reopens signed in to b, then quits.
        if upload_consumed:
            del values[layout_module.GROUP_UPLOAD_KEY]
        for key, path in ((DFRAME_STORE_KEY, ("state", "customGroupsByScope")), (GROUP_SCOPES_KEY, ("value",))):
            document = decoded(values[key])
            target = document
            for part in path:
                target = target[part]
            target["b/w"] = b_after_relaunch
            values[key] = encoded(document)
        return synchronizer, values, database, targets

    def test_old_server_groups_reloaded_before_the_upload_never_overwrite_the_source(self):
        with tempfile.TemporaryDirectory() as directory:
            synchronizer, values, database, targets = self.switched_and_relaunched(
                Path(directory), b_after_relaunch=scope("Old"), upload_consumed=False
            )
            before = dict(values)

            with database, targets:
                with self.assertRaisesRegex(LayoutChoiceError, "before Claude uploaded"):
                    synchronizer.sync()

            self.assertEqual(before, values, "nothing is written")
            self.assertEqual({"a/w": ["Focus"], "b/w": ["Old"]}, group_names(values, self.targets))

    def test_once_claude_uploaded_the_copy_the_signed_in_account_is_the_source(self):
        with tempfile.TemporaryDirectory() as directory:
            edited = scope("Focus", "New", assignments={"code:x": "id-new"})
            synchronizer, values, database, targets = self.switched_and_relaunched(
                Path(directory), b_after_relaunch=edited, upload_consumed=True
            )

            with database, targets:
                receipt = synchronizer.sync()

            self.assertEqual("synced", receipt.state)
            self.assertEqual({"a/w": ["Focus", "New"], "b/w": ["Focus", "New"]}, group_names(values, self.targets))
            snapshot = synchronizer.config.state_dir / "sidebar-layout-0.json"
            self.assertEqual({"version": 1, "adopted_scope": "b/w"}, json.loads(snapshot.read_text()))

    def test_an_account_still_holding_the_copy_is_left_alone_until_the_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            synchronizer, values, database, targets = self.switched_and_relaunched(
                Path(directory), b_after_relaunch=copy.deepcopy(self.latest), upload_consumed=False
            )

            with database, targets:
                receipt = synchronizer.sync()

            self.assertEqual("noop", receipt.state)
            self.assertEqual({"a/w": ["Focus"], "b/w": ["Focus"]}, group_names(values, self.targets))


if __name__ == "__main__":
    unittest.main()
