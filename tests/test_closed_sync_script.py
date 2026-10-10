from pathlib import Path
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "closed-sync-test.sh"
LAUNCHER = SCRIPT.with_suffix(".command")

FINISHED = '0\t{"layout":{"state":"synced"},"progress":"finished","state":"committed"}'
HEALTHY = '{"counts":{"layout_failures":0,"routine_failures":0},"progress":"finished","state":"app-running"}'

# Every command the script uses to touch Claude or the Mac. Each fake only
# records its call, so a test never quits or opens the real Claude.
FAKES = {
    "osascript": 'exit "${FAKE_QUIT_EXIT:-0}"',
    "open": "",
    "defaults": "echo 2.31226.1",
    "ps": 'case "$*" in *comm=*) echo "$FAKE_PARENT_COMMAND";; *) echo 1;; esac',
    "pgrep": (
        'left=$(cat "$STUBS/claude-checks")\n'
        '[ "$left" -gt 0 ] || exit 1\n'
        'echo $((left - 1)) > "$STUBS/claude-checks"\n'
        "echo 667"
    ),
    "tool": (
        'case "$*" in\n'
        "  --version) echo 0.5.2;;\n"
        '  "status --json")\n'
        '    head -n 1 "$STUBS/status-replies"\n'
        '    [ "$(wc -l < "$STUBS/status-replies")" -le 1 ] || sed -i "" 1d "$STUBS/status-replies";;\n'
        "  status) echo progress=waiting-for-Claude;;\n"
        '  "sync --json")\n'
        '    sleep "${FAKE_SYNC_SECONDS:-0}"\n'
        '    reply=$(head -n 1 "$STUBS/sync-replies")\n'
        '    [ "$(wc -l < "$STUBS/sync-replies")" -le 1 ] || sed -i "" 1d "$STUBS/sync-replies"\n'
        "    printf '%s\\n' \"${reply#*\t}\"\n"
        '    exit "${reply%%\t*}";;\n'
        "esac"
    ),
}


@unittest.skipUnless(shutil.which("zsh"), "zsh is required for the Mac script")
class ClosedSyncScriptTests(unittest.TestCase):
    """The closed-Claude sync test, run against fakes of Claude and the tool."""

    def start_script(
        self,
        root: Path,
        *,
        script: Path = SCRIPT,
        claude_checks: int = 3,
        sync_replies=(FINISHED,),
        status_replies=(HEALTHY,),
        parent: str = "/bin/zsh",
        tool: bool = True,
        environment=None,
    ):
        stubs = root / "bin"
        stubs.mkdir()
        for name, body in FAKES.items():
            fake = stubs / name
            fake.write_text('#!/bin/sh\necho "{} $*" >> "$STUBS/calls"\n{}\n'.format(name, body))
            fake.chmod(0o755)
        (stubs / "calls").write_text("")
        # How many times Claude still counts as open before it has quit.
        (stubs / "claude-checks").write_text(str(claude_checks))
        (stubs / "sync-replies").write_text("\n".join(sync_replies) + "\n")
        (stubs / "status-replies").write_text("\n".join(status_replies) + "\n")
        path = "{}:/usr/bin:/bin".format(stubs)
        for name in ("osascript", "open", "pgrep", "ps", "defaults"):
            self.assertEqual(str(stubs / name), shutil.which(name, path=path))
        variables = {
            "PATH": path,
            "HOME": str(root),
            "STUBS": str(stubs),
            "FAKE_PARENT_COMMAND": parent,
            "CLOSED_SYNC_TOOL": str(stubs / "tool") if tool else str(root / "missing"),
            "CLOSED_SYNC_LOG_DIR": str(root / "logs"),
            "CLOSED_SYNC_PAUSE": "0",
        }
        variables.update(environment or {})
        return subprocess.Popen(
            ["zsh", str(script)],
            env=variables,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def run_script(self, root: Path, *, answer: str = "yes\n", **options):
        process = self.start_script(root, **options)
        stdout, stderr = process.communicate(answer, timeout=60)
        result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        calls = (root / "bin" / "calls").read_text().splitlines()
        logs = sorted((root / "logs").glob("*.log")) if (root / "logs").is_dir() else []
        return result, calls, [log.read_text() for log in logs]

    def test_it_quits_claude_waits_syncs_reopens_and_keeps_the_log(self) -> None:
        with tempfile.TemporaryDirectory(prefix="closed-sync-") as directory:
            result, calls, logs = self.run_script(Path(directory))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        steps = [call for call in calls if not call.startswith(("ps ", "defaults ", "tool --version"))]
        self.assertEqual(
            [
                "pgrep -a -x Claude",
                "osascript -e tell application \"Claude\" to quit",
                "pgrep -a -x Claude",
                "pgrep -a -x Claude",
                "pgrep -a -x Claude",
                "tool sync --json",
                "tool status --json",
                "open -a Claude",
                "tool status",
            ],
            steps,
        )
        self.assertIn("RESULT: passed", result.stdout)
        self.assertEqual(1, len(logs), "one log is kept")
        for expected in (
            "Claude Session Sync 0.5.2",
            "Claude 2.31226.1",
            '"progress":"finished"',
            "sync exit code: 0",
            '"layout_failures":0',
            "progress=waiting-for-Claude",
            "RESULT: passed",
        ):
            self.assertIn(expected, logs[0])

    def test_without_a_typed_yes_claude_stays_open(self) -> None:
        with tempfile.TemporaryDirectory(prefix="closed-sync-") as directory:
            result, calls, _logs = self.run_script(Path(directory), answer="\n")

        self.assertEqual(result.returncode, 1)
        self.assertIn("Nothing was changed", result.stdout)
        self.assertEqual([], [call for call in calls if call.startswith(("osascript", "tool sync", "open"))])

    def test_it_refuses_to_start_from_inside_claude(self) -> None:
        with tempfile.TemporaryDirectory(prefix="closed-sync-") as directory:
            result, calls, _logs = self.run_script(
                Path(directory), parent="/Applications/Claude.app/Contents/Helpers/disclaimer"
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("double-click", result.stdout)
        self.assertEqual([], [call for call in calls if call.startswith(("osascript", "tool sync", "open"))])

    def test_without_the_installed_tool_claude_stays_open(self) -> None:
        with tempfile.TemporaryDirectory(prefix="closed-sync-") as directory:
            result, calls, _logs = self.run_script(Path(directory), tool=False)

        self.assertEqual(result.returncode, 1)
        self.assertIn("is not installed", result.stdout)
        self.assertEqual([], [call for call in calls if call.startswith(("osascript", "open"))])

    def test_when_claude_does_not_quit_nothing_is_synced(self) -> None:
        with tempfile.TemporaryDirectory(prefix="closed-sync-") as directory:
            result, calls, logs = self.run_script(
                Path(directory), claude_checks=1000, environment={"CLOSED_SYNC_QUIT_CHECKS": "3"}
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("Claude is still open", logs[0])
        self.assertIn("osascript -e tell application \"Claude\" to quit", calls)
        self.assertEqual([], [call for call in calls if call.startswith(("tool sync", "open"))])

    def test_when_macos_blocks_the_quit_a_manual_quit_still_continues(self) -> None:
        with tempfile.TemporaryDirectory(prefix="closed-sync-") as directory:
            result, calls, logs = self.run_script(
                Path(directory), environment={"FAKE_QUIT_EXIT": "1"}
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Quit Claude yourself with Cmd+Q", logs[0])
        self.assertIn("tool sync --json", calls)

    def test_a_failed_sync_still_reopens_claude_and_reports_it(self) -> None:
        failed = '1\t{"layout":{"reason":"unsafe-layout","state":"skipped"},"progress":"needs-attention"}'
        with tempfile.TemporaryDirectory(prefix="closed-sync-") as directory:
            result, calls, logs = self.run_script(Path(directory), sync_replies=(failed,))

        self.assertEqual(result.returncode, 1)
        self.assertEqual(["tool sync --json", "open -a Claude", "tool status"], [
            call for call in calls if call in ("tool sync --json", "open -a Claude", "tool status")
        ])
        self.assertIn("unsafe-layout", logs[0])
        self.assertIn("sync exit code: 1", logs[0])
        self.assertIn("RESULT: needs attention", logs[0])
        self.assertNotIn("RESULT: passed", logs[0])

    def test_an_installed_build_without_the_group_fix_is_named(self) -> None:
        old_build = (
            '1\t{"layout":{"detail":"custom group assignment references an unknown group",'
            '"reason":"unsafe-layout","state":"skipped"},"progress":"needs-attention"}'
        )
        with tempfile.TemporaryDirectory(prefix="closed-sync-") as directory:
            result, _calls, logs = self.run_script(Path(directory), sync_replies=(old_build,))

        self.assertEqual(result.returncode, 1)
        self.assertIn("older than 0.5.2", logs[0])
        self.assertIn("Run the installer again", logs[0])

    def test_it_waits_while_the_watcher_holds_the_sync_or_claude_shuts_down(self) -> None:
        busy = '1\t{"reason":"busy","state":"skipped"}'
        closing = '1\t{"progress":"waiting-for-Claude","reason":"claude-open","state":"waiting"}'
        with tempfile.TemporaryDirectory(prefix="closed-sync-") as directory:
            result, calls, logs = self.run_script(
                Path(directory), sync_replies=(busy, closing, FINISHED)
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(3, calls.count("tool sync --json"))
        self.assertIn("RESULT: passed", logs[0])

    def test_it_stops_waiting_for_a_sync_that_stays_busy(self) -> None:
        busy = '1\t{"reason":"busy","state":"skipped"}'
        with tempfile.TemporaryDirectory(prefix="closed-sync-") as directory:
            result, calls, logs = self.run_script(
                Path(directory), sync_replies=(busy,), environment={"CLOSED_SYNC_TRIES": "4"}
            )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(4, calls.count("tool sync --json"))
        self.assertIn("open -a Claude", calls)
        self.assertIn("RESULT: needs attention", logs[0])

    def test_pins_and_groups_that_still_fail_in_status_are_not_a_pass(self) -> None:
        unhealthy = '{"counts":{"layout_failures":1},"progress":"needs-attention","state":"app-running"}'
        with tempfile.TemporaryDirectory(prefix="closed-sync-") as directory:
            result, _calls, logs = self.run_script(Path(directory), status_replies=(unhealthy,))

        self.assertEqual(result.returncode, 1)
        self.assertIn("RESULT: needs attention", logs[0])

    def test_status_is_read_again_while_the_watcher_is_still_syncing(self) -> None:
        syncing = '{"counts":{"layout_failures":0},"progress":"syncing","state":"idle"}'
        with tempfile.TemporaryDirectory(prefix="closed-sync-") as directory:
            result, calls, logs = self.run_script(
                Path(directory), status_replies=(syncing, syncing, HEALTHY)
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(3, calls.count("tool status --json"))
        self.assertLess(calls.index("tool status --json"), calls.index("open -a Claude"))
        self.assertIn("RESULT: passed", logs[0])

    def test_a_stop_signal_during_the_sync_still_reopens_claude(self) -> None:
        with tempfile.TemporaryDirectory(prefix="closed-sync-") as directory:
            root = Path(directory)
            calls_file = root / "bin" / "calls"
            process = self.start_script(root, environment={"FAKE_SYNC_SECONDS": "2"})
            process.stdin.write("yes\n")
            process.stdin.flush()
            deadline = time.monotonic() + 30
            while "tool sync --json" not in calls_file.read_text() and time.monotonic() < deadline:
                time.sleep(0.05)
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=60)
            calls = calls_file.read_text().splitlines()

        self.assertEqual(process.returncode, 130)
        self.assertIn("tool sync --json", calls)
        self.assertEqual("open -a Claude", calls[-1])

    def test_claude_that_was_already_closed_is_not_asked_to_quit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="closed-sync-") as directory:
            result, calls, _logs = self.run_script(Path(directory), claude_checks=0)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([], [call for call in calls if call.startswith("osascript")])
        self.assertIn("tool sync --json", calls)
        self.assertIn("open -a Claude", calls)

    def test_the_double_click_file_starts_the_script(self) -> None:
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        self.assertTrue(os.access(LAUNCHER, os.X_OK), "Finder runs only an executable .command file")
        with tempfile.TemporaryDirectory(prefix="closed-sync-") as directory:
            result, calls, logs = self.run_script(Path(directory), script=LAUNCHER)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("tool sync --json", calls)
        self.assertIn("RESULT: passed", logs[0])


if __name__ == "__main__":
    unittest.main()
