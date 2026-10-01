import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from claude_session_sync.config import ApprovedTarget, Config, ConfigError, load_config
from claude_session_sync.model import Plan, Profile, SyncRequest
from claude_session_sync.planner import Planner
from claude_session_sync.store import SessionStore


class ConfigTests(unittest.TestCase):
    def write_config(self, root: Path, payload: object) -> Path:
        path = root / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def profile(self, name: str, root: Path) -> dict:
        return {
            "name": name,
            "data_root": str(root / name),
            "launch_command": ["open", "-a", name],
            "enabled": True,
            "is_default": name == "Claude",
        }

    def test_multiple_enabled_profiles_require_explicit_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.write_config(
                root,
                {
                    "version": 1,
                    "approved_targets": [],
                    "profiles": [
                        self.profile("Claude", root),
                        self.profile("Claude Personal", root),
                    ],
                    "state_dir": str(root / "state"),
                    "retention": 10,
                    "acknowledge_cross_profile_copy": False,
                    "acknowledge_cross_account_copy": False,
                    "claude_executable": "/Applications/Claude.app/Contents/MacOS/Claude",
                },
            )

            with self.assertRaisesRegex(ConfigError, "acknowledge_cross_profile_copy"):
                load_config(path)

    def test_disabled_profiles_do_not_require_cross_profile_acknowledgement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            disabled = self.profile("Claude Personal", root)
            disabled["enabled"] = False
            path = self.write_config(
                root,
                {
                    "version": 1,
                    "approved_targets": [],
                    "profiles": [self.profile("Claude", root), disabled],
                    "state_dir": str(root / "state"),
                    "retention": 10,
                    "acknowledge_cross_profile_copy": False,
                    "acknowledge_cross_account_copy": False,
                    "claude_executable": "/Applications/Claude.app/Contents/MacOS/Claude",
                },
            )

            config = load_config(path)

            self.assertEqual(["Claude"], [profile.name for profile in config.profiles])

    def test_unknown_config_and_profile_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {
                "version": 1,
                "approved_targets": [],
                "profiles": [self.profile("Claude", root)],
                "state_dir": str(root / "state"),
                "retention": 10,
                "acknowledge_cross_profile_copy": False,
                "acknowledge_cross_account_copy": False,
                "claude_executable": "/Applications/Claude.app/Contents/MacOS/Claude",
                "surprise": True,
            }
            path = self.write_config(root, payload)
            with self.assertRaisesRegex(ConfigError, "unknown config keys.*surprise"):
                load_config(path)

            del payload["surprise"]
            payload["profiles"][0]["surprise"] = True
            path = self.write_config(root, payload)
            with self.assertRaisesRegex(ConfigError, "unknown profile keys.*surprise"):
                load_config(path)

    def test_legacy_config_defaults_to_approved_only_target_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.write_config(
                root,
                {
                    "version": 1,
                    "approved_targets": [],
                    "profiles": [self.profile("Claude", root)],
                    "state_dir": str(root / "state"),
                    "retention": 10,
                    "acknowledge_cross_profile_copy": False,
                    "acknowledge_cross_account_copy": False,
                    "claude_executable": (
                        "/Applications/Claude.app/Contents/MacOS/Claude"
                    ),
                },
            )

            self.assertEqual("approved-only", load_config(path).target_policy)

    def test_automatic_target_policy_requires_cross_account_acknowledgement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.write_config(
                root,
                {
                    "version": 1,
                    "approved_targets": [],
                    "profiles": [self.profile("Claude", root)],
                    "state_dir": str(root / "state"),
                    "retention": 10,
                    "acknowledge_cross_profile_copy": False,
                    "acknowledge_cross_account_copy": False,
                    "target_policy": "all-configured-profiles",
                    "claude_executable": (
                        "/Applications/Claude.app/Contents/MacOS/Claude"
                    ),
                },
            )

            with self.assertRaisesRegex(ConfigError, "cross_account_copy"):
                load_config(path)


class PlannerTests(unittest.TestCase):
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
            approved_targets=(
                ApprovedTarget("Claude", "account", "workspace"),
                ApprovedTarget("Claude Personal", "account", "workspace"),
            ),
        )

    def target(self, data_root: Path) -> Path:
        path = data_root / "claude-code-sessions" / "account" / "workspace"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_session(
        self,
        data_root: Path,
        session_id: str,
        revision: str,
        mtime_ns: int,
        activity: int = 100,
    ) -> Path:
        path = self.target(data_root) / "local_{}.json".format(session_id)
        path.write_text(
            json.dumps(
                {
                    "sessionId": "local_" + session_id,
                    "revision": revision,
                    "lastActivityAt": activity,
                }
            ),
            encoding="utf-8",
        )
        os.utime(path, ns=(mtime_ns, mtime_ns))
        return path

    def plan(self, config: Config) -> Plan:
        return Planner(SessionStore()).plan(SyncRequest(config))

    def test_equal_activity_divergent_revisions_are_left_alone_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            self.write_session(root / "standard", "tied", "standard", 5_000_000_000)
            self.write_session(root / "personal", "tied", "personal", 9_000_000_000)
            self.write_session(root / "standard", "other", "only", 5_000_000_000)

            plan = self.plan(config)

            self.assertEqual({"tied"}, {problem.kind for problem in plan.problems})
            self.assertEqual({"tied"}, {problem.session_id for problem in plan.problems})
            self.assertEqual(["other"], [op.session_id for op in plan.operations])

    def test_multiple_account_namespaces_require_explicit_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(
                profiles=(Profile("Claude", root / "standard", ("open", "Claude")),),
                state_dir=root / "state",
                retention=10,
                acknowledge_cross_profile_copy=False,
                acknowledge_cross_account_copy=False,
                claude_executable=Path(
                    "/Applications/Claude.app/Contents/MacOS/Claude"
                ),
                approved_targets=(
                    ApprovedTarget("Claude", "work-account", "workspace"),
                    ApprovedTarget("Claude", "personal-account", "workspace"),
                ),
            )
            for account in ("work-account", "personal-account"):
                path = (
                    root / "standard" / "claude-code-sessions" / account / "workspace"
                )
                path.mkdir(parents=True)

            with self.assertRaisesRegex(ConfigError, "acknowledge_cross_account_copy"):
                self.plan(config)

    def test_latest_activity_wins_and_missing_copies_are_created(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            # The older activity has the newer file: file time plays no part.
            old = self.write_session(
                root / "standard", "overwrite", "old", 9_000_000_000, activity=100
            )
            newest = self.write_session(
                root / "personal", "overwrite", "new", 6_000_000_000, activity=200
            )
            missing_source = self.write_session(
                root / "standard", "missing", "only-copy", 7_000_000_000
            )
            self.target(root / "personal")

            plan = self.plan(config)

            self.assertEqual(
                ["missing", "overwrite"], [op.session_id for op in plan.operations]
            )
            missing, overwrite = plan.operations
            self.assertEqual(missing_source, missing.source)
            self.assertEqual(
                self.target(root / "personal") / "local_missing.json",
                missing.destination,
            )
            self.assertIsNone(missing.destination_digest_or_none)
            self.assertEqual("create", missing.kind)
            self.assertEqual(newest, overwrite.source)
            self.assertEqual(old, overwrite.destination)
            self.assertEqual("replace", overwrite.kind)
            self.assertIsNotNone(overwrite.destination_digest_or_none)
            self.assertEqual(sum(op.size for op in plan.operations), plan.total_bytes)

    def test_identical_replicas_produce_an_idempotent_noop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            self.write_session(root / "standard", "same", "same", 5_000_000_000)
            self.write_session(root / "personal", "same", "same", 9_000_000_000)

            plan = self.plan(config)

            self.assertEqual((), plan.operations)
            self.assertEqual(0, plan.total_bytes)

    def test_new_target_is_blocked_until_separately_approved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            self.write_session(root / "standard", "known", "one", 5_000_000_000)
            self.target(root / "personal")
            new_target = (
                root
                / "standard"
                / "claude-code-sessions"
                / "future-account"
                / "future-workspace"
            )
            new_target.mkdir(parents=True)

            plan = self.plan(config)

            self.assertEqual(1, len(plan.invalid_replicas))
            self.assertEqual(new_target, plan.invalid_replicas[0].path)
            self.assertIn("not approved", plan.invalid_replicas[0].reason)

    def test_automatic_target_policy_accepts_new_targets_in_configured_profiles(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(
                self.config(root),
                target_policy="all-configured-profiles",
            )
            self.write_session(root / "standard", "known", "one", 5_000_000_000)
            self.target(root / "personal")
            new_target = (
                root
                / "standard"
                / "claude-code-sessions"
                / "future-account"
                / "future-workspace"
            )
            new_target.mkdir(parents=True)

            plan = self.plan(config)

            self.assertEqual((), plan.invalid_replicas)
            self.assertTrue(
                any(operation.destination.parent == new_target for operation in plan.operations)
            )


if __name__ == "__main__":
    unittest.main()
