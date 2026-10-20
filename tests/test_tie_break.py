"""Ties where one copy already holds everything the others hold (20-10-2026: 329 chats)."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from claude_session_sync.config import ApprovedTarget, Config
from claude_session_sync.model import Profile, SyncRequest
from claude_session_sync.planner import Planner
from claude_session_sync.store import SessionStore
from claude_session_sync.tie_break import covers, tie_break

BASE = {"sessionId": "local_a", "lastActivityAt": 100, "isArchived": False}


def copy(**changes):
    return dict(BASE, **changes)


class TieBreakTests(unittest.TestCase):
    def test_an_archived_copy_wins(self):
        self.assertEqual("b", tie_break({"a": copy(), "b": copy(isArchived=True)}))

    def test_account_noise_never_blocks_a_winner(self):
        a = copy(
            steeredByRemoteClient=True, interruptedAwaitingPerson=None, lastFocusedAt=1
        )
        b = copy(
            isArchived=True, steeredByRemoteClient=None, enabledMcpTools={"x": True}
        )
        self.assertEqual("b", tie_break({"a": a, "b": b}))

    def test_a_merged_pull_request_list_covers_the_old_open_fields(self):
        # Newer Claude drops prNumber/prState/prUrl and keeps only prs.
        old = copy(
            prNumber=223,
            prState="OPEN",
            prUrl="u/223",
            prs=[{"url": "u/223", "state": "OPEN", "prNumber": 223}],
        )
        new = copy(prs=[{"url": "u/223", "state": "MERGED", "prNumber": 223}])
        self.assertEqual("new", tie_break({"old": old, "new": new}))
        self.assertFalse(covers(old, new))

    def test_a_missing_or_undismissed_pull_request_is_not_covered(self):
        one = copy(prs=[{"url": "u/1", "state": "MERGED"}])
        two = copy(prs=[{"url": "u/2", "state": "OPEN"}])
        self.assertIsNone(tie_break({"one": one, "two": two}))
        dismissed = copy(prs=[{"url": "u/1", "state": "MERGED", "dismissed": True}])
        self.assertFalse(covers(one, dismissed))

    def test_a_later_title_or_a_rename_wins(self):
        auto = copy(title="Auto name", titleSource="auto", titleTurn=0)
        renamed = copy(
            title="His name", titleSource="user", titleTurn=21, previousTitles=["x"]
        )
        self.assertEqual("renamed", tie_break({"auto": auto, "renamed": renamed}))
        same_turn = copy(title="His name", titleSource="user")
        self.assertEqual(
            "b",
            tie_break({"a": copy(title="Auto", titleSource="auto"), "b": same_turn}),
        )
        self.assertIsNone(
            tie_break(
                {
                    "a": copy(title="One", titleSource="user"),
                    "b": copy(title="Two", titleSource="user"),
                }
            )
        )

    def test_split_edits_stay_tied_so_nothing_is_lost(self):
        archived = copy(isArchived=True, title="Old", titleTurn=1)
        renamed = copy(title="New", titleTurn=5)
        self.assertIsNone(tie_break({"archived": archived, "renamed": renamed}))

    def test_folders_and_published_pages_must_all_be_held(self):
        anchor = {"gitRoot": "/r", "commonDir": "/r/.git", "trustedAt": 1}
        page = {"url": "p", "updatedAt": "2026-10-20T10:00:00Z"}
        rich = copy(isArchived=True, gitAnchors=[anchor], publishedArtifacts=[page])
        self.assertEqual("rich", tie_break({"rich": rich, "bare": copy()}))
        newer_page = copy(
            publishedArtifacts=[dict(page, updatedAt="2026-10-20T11:00:00Z")]
        )
        self.assertIsNone(tie_break({"rich": rich, "newer": newer_page}))

    def test_any_other_difference_stays_tied(self):
        self.assertIsNone(
            tie_break(
                {"a": copy(revision="a"), "b": copy(revision="b", isArchived=True)}
            )
        )


class PlannerTieBreakTests(unittest.TestCase):
    def test_the_archived_copy_replaces_the_others_instead_of_a_tie(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(
                profiles=(
                    Profile("Claude Personal", root / "personal", ("open", "Personal")),
                    Profile("Claude", root / "standard", ("open", "Claude")),
                ),
                state_dir=root / "state",
                retention=10,
                acknowledge_cross_profile_copy=True,
                acknowledge_cross_account_copy=True,
                claude_executable=Path(
                    "/Applications/Claude.app/Contents/MacOS/Claude"
                ),
                approved_targets=(
                    ApprovedTarget("Claude", "account", "workspace"),
                    ApprovedTarget("Claude Personal", "account", "workspace"),
                ),
            )
            for profile, archived in (("standard", False), ("personal", True)):
                folder = (
                    root / profile / "claude-code-sessions" / "account" / "workspace"
                )
                folder.mkdir(parents=True)
                path = folder / "local_chat.json"
                path.write_text(
                    json.dumps(
                        {
                            "sessionId": "local_chat",
                            "lastActivityAt": 100,
                            "isArchived": archived,
                        }
                    )
                )
                os.utime(path, ns=(5_000_000_000, 5_000_000_000))

            plan = Planner(SessionStore()).plan(SyncRequest(config))

            self.assertEqual((), plan.problems)
            self.assertEqual(
                [("replace", "chat")],
                [(op.kind, op.session_id) for op in plan.operations],
            )
            self.assertIn("personal", str(plan.operations[0].source))
            self.assertIn("standard", str(plan.operations[0].destination))


if __name__ == "__main__":
    unittest.main()
