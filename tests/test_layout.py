import copy
import hashlib
import json
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
    LayoutSnapshot,
    LayoutSynchronizer,
    transform_layout_records,
)
from claude_session_sync.model import Profile


def encoded(document):
    return b"\x01" + json.dumps(document, separators=(",", ":")).encode("utf-8")


def decoded(value):
    return json.loads(value[1:].decode("utf-8"))


def scope(*groups, assignments=None):
    entries = [
        {"id": "id-{}".format(name.lower()), "name": name} for name in groups
    ]
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
        first = scope(
            "Focus", "Backlog", assignments={"code:local_one": "id-focus"}
        )
        second = scope(
            "Focus", "Backlog", assignments={"code:local_one": "id-backlog"}
        )
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
            [group["name"] for group in store["customGroupsByScope"]["new/w"]["groups"]],
        )

    def test_unrelated_store_fields_are_preserved(self):
        current = records(
            {"a/w": scope("Focus")}, active="a/w", extra={"keep": [1, 2, 3]}
        )
        result = transform_layout_records(
            current, {"a/w": set()}, timestamp_ms=10
        )

        self.assertEqual(
            {"keep": [1, 2, 3]},
            decoded(result.records[DFRAME_STORE_KEY])["state"]["unrelated"],
        )

    def test_disagreeing_group_records_stop_without_a_replacement(self):
        current = records({"a/w": scope("Focus")}, active="a/w")
        group_record = decoded(current[GROUP_SCOPES_KEY])
        group_record["value"] = {}
        current[GROUP_SCOPES_KEY] = encoded(group_record)

        with self.assertRaisesRegex(LayoutError, "disagree"):
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

            synchronizer = LayoutSynchronizer(config, helper=root / "helper")
            with patch("claude_session_sync.layout.LevelDatabase", FakeDatabase):
                synchronizer._recover_pending()

            self.assertEqual(
                "ROLLED_BACK",
                json.loads(journal_path.read_text(encoding="utf-8"))["state"],
            )


if __name__ == "__main__":
    unittest.main()
