import subprocess
import unittest
from unittest.mock import patch
from pathlib import Path

from claude_session_sync.model import Profile
from claude_session_sync.processes import (
    DEFAULT_CLAUDE_EXECUTABLE,
    ProcessProbe,
    managed_processes,
    parse_process_table,
)


class ProcessParsingTests(unittest.TestCase):
    def test_only_exact_executable_and_managed_profile_are_reported(self):
        executable = Path("/Applications/Claude.app/Contents/MacOS/Claude")
        table = "\n".join(
            (
                '101    /Applications/Claude.app/Contents/MacOS/Claude --user-data-dir="/tmp/Work Profile"',
                "102 /Applications/Claude.app/Contents/MacOS/Claude --user-data-dir /tmp/personal",
                "103 /Applications/Claude.app/Contents/MacOS/Claude Helper --user-data-dir=/tmp/work",
                "104 /Applications/Claude.app/Contents/MacOS/Claude-pretender --user-data-dir=/tmp/work",
                "105 /Applications/Claude.app/Contents/MacOS/Claude --user-data-dir=/tmp/unmanaged",
                "106 /Applications/Claude.app/Contents/MacOS/Claude --user-data-dir=/tmp/Work Profile",
            )
        )

        processes = parse_process_table(
            table,
            executable=executable,
            managed_profile_roots=(Path("/tmp/Work Profile"), Path("/tmp/personal")),
            default_profile_root=Path("/tmp/default"),
        )

        self.assertEqual([process.pid for process in processes], [101, 102, 106])
        self.assertEqual(
            [process.user_data_dir for process in processes],
            [
                Path("/tmp/Work Profile"),
                Path("/tmp/personal"),
                Path("/tmp/Work Profile"),
            ],
        )

    def test_missing_profile_argument_is_classified_as_configured_default(self):
        executable = Path("/Applications/Claude.app/Contents/MacOS/Claude")
        table = "\n".join(
            (
                "not-a-pid /Applications/Claude.app/Contents/MacOS/Claude --user-data-dir=/tmp/work",
                "/Applications/Claude.app/Contents/MacOS/Claude --user-data-dir=/tmp/work",
                "201 /Applications/Claude.app/Contents/MacOS/Claude",
                "202 'unterminated",
            )
        )

        processes = parse_process_table(
            table,
            executable=executable,
            managed_profile_roots=(Path("/tmp/work"), Path("/tmp/default")),
            default_profile_root=Path("/tmp/default"),
        )

        self.assertEqual([process.pid for process in processes], [201])
        self.assertEqual(processes[0].user_data_dir, Path("/tmp/default"))


class ProcessProbeTests(unittest.TestCase):
    def test_transient_read_timeout_retries_within_the_original_budget(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(kwargs["timeout"])
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return subprocess.CompletedProcess(command, 0, stdout="301 /Applications/Claude.app/Contents/MacOS/Claude\n")

        probe = ProcessProbe(runner=runner)
        result = probe.running(executable=DEFAULT_CLAUDE_EXECUTABLE,
            managed_profile_roots=(Path("/tmp/work"),), default_profile_root=Path("/tmp/work"), timeout=5)
        self.assertEqual([301], [item.pid for item in result])
        self.assertEqual(2, len(calls))
        self.assertEqual(2.0, calls[0])
        self.assertGreater(calls[1], 0)
        self.assertLessEqual(calls[1], 5)

    def test_repeated_read_timeout_stays_bounded_and_never_means_app_closed(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(kwargs["timeout"])
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        with self.assertRaises(subprocess.TimeoutExpired):
            ProcessProbe(runner=runner).running(executable=DEFAULT_CLAUDE_EXECUTABLE,
                managed_profile_roots=(Path("/tmp/work"),), timeout=5)
        self.assertEqual(2, len(calls))

    def test_exhausted_budget_does_not_start_another_read(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        with patch("claude_session_sync.processes.time.monotonic", side_effect=(100, 105)):
            with self.assertRaises(subprocess.TimeoutExpired):
                ProcessProbe(runner=runner).running(executable=DEFAULT_CLAUDE_EXECUTABLE,
                    managed_profile_roots=(Path("/tmp/work"),), timeout=5)
        self.assertEqual(1, len(calls))

    def test_probe_reads_process_table_through_injected_runner(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="301 /Applications/Claude.app/Contents/MacOS/Claude --user-data-dir=/tmp/work\n",
                stderr="",
            )

        probe = ProcessProbe(runner=runner)
        processes = probe.running(
            executable=Path("/Applications/Claude.app/Contents/MacOS/Claude"),
            managed_profile_roots=(Path("/tmp/work"),),
            default_profile_root=None,
            timeout=0.25,
        )

        self.assertEqual([process.pid for process in processes], [301])
        self.assertEqual(calls[0][0], ["ps", "-axo", "pid=,command="])
        self.assertTrue(calls[0][1]["check"])
        self.assertTrue(calls[0][1]["text"])
        self.assertTrue(calls[0][1]["capture_output"])
        self.assertEqual(calls[0][1]["timeout"], 0.25)

    def test_config_tuple_keeps_process_executable_separate_from_launch_command(self):
        class RecordingProbe:
            def __init__(self):
                self.arguments = None

            def running(self, **kwargs):
                self.arguments = kwargs
                return ()

        probe = RecordingProbe()
        profiles = (
            Profile(
                "Work",
                Path("/tmp/work"),
                ("/usr/bin/open", "-a", "Claude"),
                True,
            ),
            Profile(
                "Personal",
                Path("/tmp/personal"),
                (
                    "/usr/bin/open",
                    "-a",
                    "Claude",
                    "--args",
                    "--user-data-dir=/tmp/personal",
                ),
            ),
        )

        self.assertEqual(managed_processes(profiles, probe=probe), ())
        self.assertEqual(probe.arguments["executable"], DEFAULT_CLAUDE_EXECUTABLE)
        self.assertEqual(
            tuple(probe.arguments["managed_profile_roots"]),
            (Path("/tmp/work"), Path("/tmp/personal")),
        )
        self.assertEqual(probe.arguments["default_profile_root"], Path("/tmp/work"))

    def test_app_bundle_commands_use_explicit_process_identity(self):
        class RecordingProbe:
            def __init__(self):
                self.arguments = None

            def running(self, **kwargs):
                self.arguments = kwargs
                return ()

        probe = RecordingProbe()
        profiles = (
            Profile(
                "Work",
                Path("/tmp/work"),
                ("/usr/bin/open", "-a", "/Applications/Claude.app"),
                True,
            ),
            Profile(
                "Personal",
                Path("/tmp/personal"),
                ("/usr/bin/open", "-a", "/Applications/Claude Personal.app"),
                False,
            ),
        )

        self.assertEqual(managed_processes(profiles, probe=probe), ())
        self.assertEqual(probe.arguments["default_profile_root"], Path("/tmp/work"))


if __name__ == "__main__":
    unittest.main()
