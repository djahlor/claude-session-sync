import json
import tempfile
import time
import unittest
from pathlib import Path

from claude_session_sync.config import ApprovedTarget, Config
from claude_session_sync.model import Profile, SyncRequest
from claude_session_sync.planner import Planner


class PerformanceTests(unittest.TestCase):
    def test_warm_plan_of_five_thousand_replicas_finishes_within_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile_roots = (root / "Claude", root / "Claude-Personal")
            targets = []
            for profile_root in profile_roots:
                target = profile_root / "claude-code-sessions" / "account" / "workspace"
                target.mkdir(parents=True)
                targets.append(target)
            for index in range(2_500):
                session_id = "benchmark-{:04d}".format(index)
                content = json.dumps({"sessionId": session_id, "index": index}).encode()
                for target in targets:
                    (target / "local_{}.json".format(session_id)).write_bytes(content)

            config = Config(
                profiles=(
                    Profile("Work", profile_roots[0], ("/usr/bin/true",)),
                    Profile("Personal", profile_roots[1], ("/usr/bin/true",)),
                ),
                state_dir=root / "state",
                retention=10,
                acknowledge_cross_profile_copy=True,
                acknowledge_cross_account_copy=True,
                claude_executable=Path(
                    "/Applications/Claude.app/Contents/MacOS/Claude"
                ),
                approved_targets=(
                    ApprovedTarget("Work", "account", "workspace"),
                    ApprovedTarget("Personal", "account", "workspace"),
                ),
            )
            planner = Planner()
            cold = planner.plan(SyncRequest(config))
            self.assertEqual((), cold.operations)

            started = time.monotonic()
            warm = planner.plan(SyncRequest(config))
            elapsed = time.monotonic() - started

            self.assertEqual((), warm.operations)
            self.assertLess(
                elapsed, 3.0, "warm 5,000-replica plan took {:.3f}s".format(elapsed)
            )


if __name__ == "__main__":
    unittest.main()
