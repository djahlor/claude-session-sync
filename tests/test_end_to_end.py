import json
import tempfile
import unittest
from pathlib import Path

from claude_session_sync.config import ApprovedTarget, Config
from claude_session_sync.model import Profile, SyncRequest
from claude_session_sync.planner import Planner
from claude_session_sync.transaction import TransactionEngine


def _replica(target: Path, session_id: str, marker: str) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    path = target / "local_{}.json".format(session_id)
    path.write_text(
        json.dumps({"sessionId": session_id, "marker": marker}),
        encoding="utf-8",
    )
    return path


class EndToEndTests(unittest.TestCase):
    def test_two_profiles_converge_then_rollback_to_the_exact_initial_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / "Claude"
            personal = root / "Claude-Personal"
            work_one = work / "claude-code-sessions" / "account-a" / "workspace-a"
            work_two = work / "claude-code-sessions" / "account-b" / "workspace-b"
            personal_one = (
                personal
                / "claude-code-sessions"
                / "account-personal"
                / "workspace-personal"
            )
            _replica(work_one, "work-session", "work")
            _replica(personal_one, "personal-session", "personal")
            work_two.mkdir(parents=True)

            config = Config(
                profiles=(
                    Profile("Work", work, ("/usr/bin/true",)),
                    Profile("Personal", personal, ("/usr/bin/true",)),
                ),
                state_dir=root / "state",
                retention=10,
                acknowledge_cross_profile_copy=True,
                acknowledge_cross_account_copy=True,
                claude_executable=Path(
                    "/Applications/Claude.app/Contents/MacOS/Claude"
                ),
                approved_targets=(
                    ApprovedTarget("Work", "account-a", "workspace-a"),
                    ApprovedTarget("Work", "account-b", "workspace-b"),
                    ApprovedTarget(
                        "Personal", "account-personal", "workspace-personal"
                    ),
                ),
            )
            planner = Planner()
            engine = TransactionEngine(config.state_dir, process_probe=lambda: False)

            initial = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("local_*.json")
            }
            plan = planner.plan(SyncRequest(config))
            self.assertEqual(4, len(plan.operations))
            receipt = engine.apply(plan)
            self.assertEqual("committed", receipt.status)

            converged = planner.plan(SyncRequest(config))
            self.assertEqual((), converged.operations)
            for target in (work_one, work_two, personal_one):
                self.assertEqual(
                    {"local_personal-session.json", "local_work-session.json"},
                    {path.name for path in target.glob("local_*.json")},
                )

            recovery = engine.rollback(receipt.run_id)
            self.assertEqual("rolled_back", recovery.status)
            restored = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("local_*.json")
            }
            self.assertEqual(initial, restored)


if __name__ == "__main__":
    unittest.main()
