"""Stopping the "Artifact comment monitor ... didn't resume" warnings (03-11-2026)."""

import io
import json
import os
import stat
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from claude_session_sync import cli
from claude_session_sync.artifact_warnings import (
    BACKUP_FILENAME,
    OWNER_FILENAME,
    SWITCH,
    OpenChats,
    SettingsError,
    TranscriptCleaner,
    add_switch,
    ensure_switch,
    parse_open_chats,
    release_switch,
    remove_switch,
    run_artifact_warnings,
    status_fields,
)
from claude_session_sync.cli import CliDependencies, run
from claude_session_sync.config import Config, load_config
from claude_session_sync.model import Profile

SESSION = "3f2a1b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b"
OTHER = "9e8d7c6b-5a49-4382-9716-a5b4c3d2e1f0"
PAGE = "11111111-2222-4333-8444-555555555555"
LINE = f'    "{SWITCH}": "0"'
SYNCED = {"bytes": 0, "counts": {"operations": 0, "planned": 0}, "state": "noop"}


def monitor(session=SESSION, pages=(PAGE,)):
    """One saved watch, as Claude Code 2.1.309 writes it after publishing."""

    record = {
        "type": "artifact-comment-monitor",
        "v": 1,
        "sessionId": session,
        "artifacts": {
            page: {"state": "watching", "writtenAtMs": 1790000000000, "title": "Page"}
            for page in pages
        },
    }
    return (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")


def message(text):
    line = {"type": "user", "message": {"role": "user", "content": text}}
    return (json.dumps(line, separators=(",", ":")) + "\n").encode("utf-8")


def config(root, **changes):
    return replace(
        Config(
            profiles=(Profile("Work", root / "Claude", ("/usr/bin/true",), True),),
            state_dir=root / "state",
            retention=5,
            acknowledge_cross_profile_copy=False,
            acknowledge_cross_account_copy=True,
            claude_executable=Path("/usr/bin/true"),
        ),
        **changes,
    )


def age(path, seconds=3600):
    """Make a transcript look saved long ago, by a chat that is closed."""

    then = time.time() - seconds
    os.utime(path, (then, then))


class AddSwitchTests(unittest.TestCase):
    def test_adds_the_switch_to_the_env_block_and_keeps_every_other_byte(self):
        text = (
            "{\n"
            '  "env": {\n'
            '    "EXISTING": "1"\n'
            "  },\n"
            '  "permissions": {\n'
            '    "allow": ["Bash(ls:*)"]\n'
            "  }\n"
            "}\n"
        )
        changed, created = add_switch(text)
        self.assertEqual(
            "{\n"
            '  "env": {\n'
            '    "EXISTING": "1",\n'
            f"{LINE}\n"
            "  },\n"
            '  "permissions": {\n'
            '    "allow": ["Bash(ls:*)"]\n'
            "  }\n"
            "}\n",
            changed,
        )
        self.assertFalse(created)

    def test_adds_an_env_block_when_there_is_none(self):
        text = '{\n  "model": "opus",\n  "permissions": {\n    "allow": []\n  }\n}\n'
        changed, created = add_switch(text)
        self.assertEqual(
            '{\n  "model": "opus",\n  "permissions": {\n    "allow": []\n  },\n'
            f'  "env": {{\n{LINE}\n  }}\n}}\n',
            changed,
        )
        self.assertTrue(created)

    def test_fills_an_empty_env_block_and_an_empty_file(self):
        changed, _created = add_switch('{\n  "env": {},\n  "model": "opus"\n}\n')
        self.assertEqual(
            f'{{\n  "env": {{\n{LINE}\n  }},\n  "model": "opus"\n}}\n', changed
        )
        changed, _created = add_switch("{}\n")
        self.assertEqual(f'{{\n  "env": {{\n{LINE}\n  }}\n}}\n', changed)

    def test_keeps_a_compact_file_on_one_line_and_tab_indentation(self):
        changed, _created = add_switch('{"env":{"A":"1"},"model":"opus"}')
        self.assertEqual(
            f'{{"env":{{"A":"1","{SWITCH}":"0"}},"model":"opus"}}', changed
        )
        changed, _created = add_switch('{\n\t"model": "opus"\n}\n')
        self.assertEqual(
            f'{{\n\t"model": "opus",\n\t"env": {{\n\t\t"{SWITCH}": "0"\n\t}}\n}}\n',
            changed,
        )

    def test_keeps_strings_that_look_like_json_and_escaped_keys(self):
        text = '{\n  "statusLine": "{ \\"env\\": } ,",\n  "\\u0065nv": {"A": "}"}\n}\n'
        changed, created = add_switch(text)
        self.assertFalse(created)
        self.assertEqual(
            {"statusLine": '{ "env": } ,', "env": {"A": "}", SWITCH: "0"}},
            json.loads(changed),
        )
        self.assertTrue(changed.startswith(text[: text.index('"}"}') + 3]))

    def test_does_nothing_when_the_switch_is_already_there_with_any_value(self):
        for value in ("0", "1", ""):
            with self.subTest(value=value):
                text = json.dumps({"env": {SWITCH: value}}, indent=2)
                self.assertEqual((None, False), add_switch(text))

    def test_refuses_an_env_that_is_not_an_object(self):
        with self.assertRaises(SettingsError) as raised:
            add_switch('{"env": "A=1"}')
        self.assertEqual("settings-unrecognised", raised.exception.reason)


class SettingsFileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = self.root / "state"
        self.settings = self.root / "claude" / "settings.json"
        self.settings.parent.mkdir()

    def write(self, text, mode=0o644):
        self.settings.write_text(text, encoding="utf-8")
        os.chmod(self.settings, mode)
        return text

    def test_on_then_off_gives_back_the_exact_file(self):
        originals = (
            '{\n  "env": {\n    "EXISTING": "1"\n  },\n  "model": "opus"\n}\n',
            '{\n  "model": "opus"\n}\n',
            '{\n  "env": {},\n  "model": "opus"\n}\n',
            '{\n  "env": { \n  },\n  "model": "opus"\n}\n',
            "{}",
            "{\r\n  }\r\n",
        )
        for original in originals:
            with self.subTest(original=original):
                self.write(original)
                self.assertEqual("added", ensure_switch(self.state, self.settings))
                self.assertEqual(
                    "0", json.loads(self.settings.read_text())["env"][SWITCH]
                )
                self.assertEqual("removed", release_switch(self.state))
                self.assertEqual(original.encode(), self.settings.read_bytes())
                self.assertFalse((self.state / OWNER_FILENAME).exists())

    def test_keeps_the_file_mode(self):
        self.write('{"model": "opus"}', mode=0o640)
        ensure_switch(self.state, self.settings)
        self.assertEqual(0o640, stat.S_IMODE(os.stat(self.settings).st_mode))

    def test_a_switch_already_there_is_never_changed_or_taken_out(self):
        for value, result in (("0", "present"), ("1", "other-value")):
            with self.subTest(value=value):
                text = self.write(
                    f'{{\n  "env": {{\n    "{SWITCH}": "{value}"\n  }}\n}}\n'
                )
                before = os.stat(self.settings)
                self.assertEqual(result, ensure_switch(self.state, self.settings))
                self.assertEqual(text, self.settings.read_text())
                self.assertEqual(before.st_ino, os.stat(self.settings).st_ino)
                self.assertIsNone(release_switch(self.state))
                self.assertEqual(text, self.settings.read_text())

    def test_turning_off_takes_out_only_the_switch_sync_put_in(self):
        self.write('{\n  "model": "opus"\n}\n')
        ensure_switch(self.state, self.settings)
        # Added by hand after sync made the env block.
        text = self.settings.read_text().replace(
            f'"{SWITCH}": "0"', f'"{SWITCH}": "0",\n    "MINE": "1"'
        )
        self.write(text)

        self.assertEqual("removed", release_switch(self.state))

        self.assertEqual(
            {"model": "opus", "env": {"MINE": "1"}},
            json.loads(self.settings.read_text()),
        )

    def test_turning_off_leaves_a_switch_someone_changed(self):
        self.write('{\n  "model": "opus"\n}\n')
        ensure_switch(self.state, self.settings)
        text = self.write(self.settings.read_text().replace('"0"', '"1"'))

        self.assertEqual("left", release_switch(self.state))

        self.assertEqual(text, self.settings.read_text())
        self.assertFalse((self.state / OWNER_FILENAME).exists())

    def test_creates_a_missing_settings_file_privately(self):
        self.assertEqual("added", ensure_switch(self.state, self.settings))
        self.assertEqual({"env": {SWITCH: "0"}}, json.loads(self.settings.read_text()))
        self.assertEqual(0o600, stat.S_IMODE(os.stat(self.settings).st_mode))

    def test_leaves_a_linked_or_unreadable_settings_file_alone(self):
        target = self.root / "dotfiles.json"
        target.write_text('{"model": "opus"}')
        self.settings.symlink_to(target)
        with self.assertRaises(SettingsError) as raised:
            ensure_switch(self.state, self.settings)
        self.assertEqual("settings-link", raised.exception.reason)
        self.assertEqual('{"model": "opus"}', target.read_text())
        self.settings.unlink()

        text = self.write('{\n  // a comment\n  "model": "opus"\n}\n')
        with self.assertRaises(SettingsError) as raised:
            ensure_switch(self.state, self.settings)
        self.assertEqual("settings-unreadable", raised.exception.reason)
        self.assertEqual(text, self.settings.read_text())
        self.assertFalse((self.state / OWNER_FILENAME).exists())

    def test_a_file_changed_after_it_was_read_is_not_overwritten(self):
        from claude_session_sync import artifact_warnings

        self.write('{"model": "opus"}')
        real_read = artifact_warnings._read_settings

        def read_then_claude_writes(path):
            result = real_read(path)
            self.write('{"model": "sonnet", "extra": 1}')
            return result

        with mock.patch.object(
            artifact_warnings, "_read_settings", side_effect=read_then_claude_writes
        ):
            with self.assertRaises(SettingsError) as raised:
                ensure_switch(self.state, self.settings)

        self.assertEqual("settings-changed", raised.exception.reason)
        self.assertEqual('{"model": "sonnet", "extra": 1}', self.settings.read_text())
        self.assertFalse((self.state / OWNER_FILENAME).exists())

    def test_an_error_after_the_new_file_is_in_place_keeps_the_record(self):
        from claude_session_sync import artifact_warnings

        original = self.write('{\n  "model": "opus"\n}\n', mode=0o644)
        real_fsync = artifact_warnings.fsync_directory

        def failing_fsync(path):
            if Path(path) == self.settings.parent:
                raise OSError(5, "Input/output error")
            real_fsync(path)

        with mock.patch.object(
            artifact_warnings, "fsync_directory", side_effect=failing_fsync
        ):
            with self.assertRaises(OSError):
                ensure_switch(self.state, self.settings)

        self.assertEqual("0", json.loads(self.settings.read_text())["env"][SWITCH])
        self.assertEqual(0o644, stat.S_IMODE(os.stat(self.settings).st_mode))
        self.assertTrue((self.state / OWNER_FILENAME).exists())
        self.assertEqual("removed", release_switch(self.state))
        self.assertEqual(original, self.settings.read_text())

    def test_remove_switch_is_the_inverse_of_add_switch(self):
        text = '{\n  "env": {\n    "A": "1",\n    "B": "2"\n  }\n}\n'
        changed, created = add_switch(text)
        self.assertEqual(text, remove_switch(changed, created_env=created))
        self.assertIsNone(remove_switch(text, created_env=False))


class CleanerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = self.root / "state"
        self.projects = self.root / "projects"
        self.folder = self.projects / "-Users-me-repo"
        self.folder.mkdir(parents=True)

    def transcript(self, session, *lines, old=True):
        path = self.folder / f"{session}.jsonl"
        path.write_bytes(b"".join(lines))
        if old:
            age(path)
        return path

    def clean(self, chats=OpenChats()):
        return TranscriptCleaner(self.state, self.projects, chats).clean()

    def backup(self):
        path = self.state / BACKUP_FILENAME
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_removes_only_saved_watch_lines_and_backs_each_one_up(self):
        mention = message('the type is {"type":"artifact-comment-monitor"} here')
        kept = [message("hello"), mention, message("bye")]
        path = self.transcript(
            SESSION,
            kept[0],
            monitor(),
            kept[1],
            monitor(pages=(PAGE, OTHER)),
            kept[2],
        )
        os.chmod(path, 0o640)

        receipt = self.clean()

        self.assertEqual(
            (2, 1, 0), (receipt.records, receipt.chats, receipt.open_chats)
        )
        self.assertEqual(b"".join(kept), path.read_bytes())
        self.assertEqual(0o640, stat.S_IMODE(os.stat(path).st_mode))
        backup = self.backup()
        self.assertEqual([1, 3], [entry["line"] for entry in backup])
        self.assertEqual({str(path)}, {entry["file"] for entry in backup})
        self.assertEqual(
            [monitor(), monitor(pages=(PAGE, OTHER))],
            [entry["raw"].encode("utf-8") for entry in backup],
        )
        backup_mode = os.stat(self.state / BACKUP_FILENAME).st_mode
        self.assertEqual(0o600, stat.S_IMODE(backup_mode))

    def test_leaves_files_without_saved_watches_untouched(self):
        mention = message('{"type":"artifact-comment-monitor"} is only quoted')
        path = self.transcript(SESSION, message("hello"), mention)
        before = os.stat(path)

        receipt = self.clean()

        after = os.stat(path)
        self.assertEqual(
            (0, 0, 0), (receipt.records, receipt.chats, receipt.open_chats)
        )
        self.assertEqual(message("hello") + mention, path.read_bytes())
        self.assertEqual(
            (before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns)
        )
        self.assertFalse((self.state / BACKUP_FILENAME).exists())

    def test_skips_a_chat_whose_claude_code_process_still_runs(self):
        path = self.transcript(SESSION, message("hello"), monitor())
        other = self.transcript(OTHER, monitor(OTHER), message("hi"))
        before = path.read_bytes()

        receipt = self.clean(OpenChats(frozenset({SESSION})))

        self.assertEqual(
            (1, 1, 1), (receipt.records, receipt.chats, receipt.open_chats)
        )
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(message("hi"), other.read_bytes())

        # Once that chat has closed, the next sync clears it too.
        receipt = self.clean()
        self.assertEqual(
            (1, 1, 0), (receipt.records, receipt.chats, receipt.open_chats)
        )
        self.assertEqual(message("hello"), path.read_bytes())

    def test_skips_a_transcript_saved_moments_ago_or_after_a_claude_code_started(
        self,
    ):
        recent = self.transcript(SESSION, monitor(), old=False)
        self.assertEqual(1, self.clean().open_chats)
        self.assertEqual(monitor(), recent.read_bytes())

        age(recent, 600)
        running = OpenChats(running_since=time.time() - 3600)
        self.assertEqual(1, self.clean(running).open_chats)
        self.assertEqual(monitor(), recent.read_bytes())

        self.assertEqual(1, self.clean(OpenChats(running_since=time.time())).records)

    def test_a_forked_chat_is_skipped_although_its_process_names_another(self):
        # `--resume X --fork-session` writes to a new transcript, not to X.
        forked = self.transcript(OTHER, monitor(OTHER))
        age(forked, 120)
        listing = f"  10:00 /opt/claude-code/claude --resume {SESSION} --fork-session"

        receipt = self.clean(parse_open_chats(listing, time.time()))

        self.assertEqual((0, 1), (receipt.records, receipt.open_chats))
        self.assertEqual(monitor(OTHER), forked.read_bytes())

    def test_stops_cleaning_when_claude_opens_again(self):
        path = self.transcript(SESSION, message("a"), monitor())

        receipt = TranscriptCleaner(
            self.state, self.projects, OpenChats(), claude_running=lambda: True
        ).clean()

        self.assertEqual((0, 1), (receipt.records, receipt.open_chats))
        self.assertEqual(message("a") + monitor(), path.read_bytes())
        self.assertEqual(1, self.clean().records)

    def test_a_line_still_being_written_stays(self):
        partial = monitor()[:-1]
        path = self.transcript(SESSION, monitor(), message("a"), partial)

        self.assertEqual(1, self.clean().records)

        self.assertEqual(message("a") + partial, path.read_bytes())

    def test_reads_each_line_once_and_finds_watches_added_later(self):
        path = self.transcript(SESSION, message("a"))
        self.assertEqual(0, self.clean().records)

        with path.open("ab") as stream:
            stream.write(monitor())
        age(path)

        self.assertEqual(1, self.clean().records)
        self.assertEqual(message("a"), path.read_bytes())
        offsets = json.loads(
            (self.state / "artifact-warnings-offsets.json").read_text()
        )
        self.assertEqual(len(message("a")), offsets["files"][str(path)]["scanned"])

    def test_an_unchanged_transcript_is_not_read_again(self):
        from claude_session_sync import artifact_warnings

        self.transcript(SESSION, message("a"), monitor())
        self.assertEqual(1, self.clean().records)
        seen = []
        real_scan = artifact_warnings._scan

        def scan(path, start):
            seen.append(start)
            return real_scan(path, start)

        with mock.patch.object(artifact_warnings, "_scan", side_effect=scan):
            self.assertEqual(0, self.clean().records)
        self.assertEqual([len(message("a"))], seen)

    def test_never_follows_a_linked_transcript_or_folder(self):
        outside = self.root / "outside.jsonl"
        outside.write_bytes(monitor())
        age(outside)
        (self.folder / f"{OTHER}.jsonl").symlink_to(outside)
        (self.projects / "linked-folder").symlink_to(
            self.root, target_is_directory=True
        )

        self.assertEqual(0, self.clean().records)

        self.assertEqual(monitor(), outside.read_bytes())


class OpenChatsTests(unittest.TestCase):
    def test_reads_chat_ids_and_when_the_oldest_claude_code_started(self):
        now = 1_000_000.0
        unnamed = (
            "   1-00:00:00 bun /Users/me/.claude/hooks/EventLogger.hook.ts",
            "      05:00 /usr/bin/python3 /Users/me/Library/Application Support/"
            "ClaudeSessionSync/bin/claude-session-sync auto",
            "      02:00 claude",
            "      09:00 node /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js",
        )
        named = (
            "   01:02:03 /Users/me/Library/Application Support/Claude/claude-code/"
            f"2.1.309/abc/claude.app/Contents/MacOS/claude --verbose --resume={SESSION} --",
            f"      00:10 claude --session-id {OTHER.upper()}",
        )

        self.assertEqual(
            OpenChats(frozenset(), now - 540),
            parse_open_chats("\n".join(unnamed), now),
        )
        self.assertEqual(
            OpenChats(frozenset({SESSION, OTHER}), now - 3723),
            parse_open_chats("\n".join(named + unnamed), now),
        )

    def test_no_claude_code_means_nothing_is_open(self):
        chats = parse_open_chats("  00:01 /bin/zsh\n  00:02 /usr/sbin/cfprefsd\n", 1.0)
        self.assertEqual(OpenChats(), chats)


class RunTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.settings = self.root / "claude" / "settings.json"
        self.settings.parent.mkdir()
        self.original = '{\n  "model": "opus"\n}\n'
        self.settings.write_text(self.original)
        self.folder = self.root / "claude" / "projects" / "-Users-me-repo"
        self.folder.mkdir(parents=True)
        self.path = self.folder / f"{SESSION}.jsonl"
        self.path.write_bytes(message("a") + monitor())
        age(self.path)

    def run_step(self, loaded, running=lambda: False):
        return run_artifact_warnings(
            loaded,
            running,
            settings=self.settings,
            projects=self.root / "claude" / "projects",
            chats=OpenChats,
        )

    def test_off_by_default_changes_nothing(self):
        loaded = config(self.root)
        self.assertFalse(loaded.stop_artifact_warnings)

        self.assertEqual({}, self.run_step(loaded))

        self.assertEqual(self.original, self.settings.read_text())
        self.assertEqual(message("a") + monitor(), self.path.read_bytes())
        self.assertEqual(
            {"artifact_warnings": "off", "cleared": 0}, status_fields(loaded)
        )

    def test_on_sets_the_switch_clears_watches_and_off_takes_the_switch_out(self):
        loaded = config(self.root, stop_artifact_warnings=True)

        result = self.run_step(loaded)

        self.assertEqual(
            {
                "state": "done",
                "switch": "added",
                "records": 1,
                "chats": 1,
                "open_chats": 0,
            },
            result,
        )
        self.assertEqual("0", json.loads(self.settings.read_text())["env"][SWITCH])
        self.assertEqual(message("a"), self.path.read_bytes())
        self.assertEqual(
            {"artifact_warnings": "on", "cleared": 1}, status_fields(loaded)
        )

        again = self.run_step(loaded)
        self.assertEqual("noop", again["state"])
        self.assertEqual(
            {"artifact_warnings": "on", "cleared": 0}, status_fields(loaded)
        )

        off = replace(loaded, stop_artifact_warnings=False)
        self.assertEqual({"state": "done", "switch": "removed"}, self.run_step(off))
        self.assertEqual(self.original, self.settings.read_text())
        self.assertEqual({}, self.run_step(off))

    def test_claude_open_again_leaves_the_transcripts(self):
        loaded = config(self.root, stop_artifact_warnings=True)

        result = self.run_step(loaded, running=lambda: True)

        self.assertEqual(
            ("deferred", "added", 0),
            (result["state"], result["switch"], result["records"]),
        )
        self.assertEqual(message("a") + monitor(), self.path.read_bytes())

    def test_a_settings_problem_is_reported_and_the_transcripts_still_clean(self):
        self.settings.write_text("not json")
        loaded = config(self.root, stop_artifact_warnings=True)

        result = self.run_step(loaded)

        self.assertEqual(
            ("skipped", "settings-unreadable"), (result["state"], result["reason"])
        )
        self.assertEqual(1, result["records"])
        self.assertEqual(
            {
                "artifact_warnings": "on",
                "cleared": 1,
                "artifact_warnings_reason": "settings-unreadable",
            },
            status_fields(loaded),
        )


class CliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "Claude").mkdir()

    def test_configure_turns_the_option_on(self):
        config_path = self.root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "profiles": [
                        {
                            "name": "Work",
                            "data_root": str(self.root / "Claude"),
                            "launch_command": ["/usr/bin/true"],
                            "is_default": True,
                        }
                    ],
                    "approved_targets": [],
                    "state_dir": str(self.root / "state"),
                    "retention": 5,
                    "acknowledge_cross_profile_copy": False,
                    "acknowledge_cross_account_copy": False,
                    "claude_executable": "/usr/bin/true",
                }
            )
        )
        self.assertFalse(load_config(config_path).stop_artifact_warnings)
        output = io.StringIO()

        code = run(
            [
                "--config",
                str(config_path),
                "configure",
                "--stop-artifact-warnings",
                "--apply",
            ],
            stdout=output,
            stderr=io.StringIO(),
        )

        self.assertEqual(0, code)
        self.assertTrue(load_config(config_path).stop_artifact_warnings)
        self.assertIn("'artifact_warnings_stopped': 1", output.getvalue())

    def command(self, loaded, *arguments, step=None):
        calls = []

        def artifact_warnings(config, running):
            calls.append(running())
            return step(config, running) if step else {}

        output = io.StringIO()
        with mock.patch.object(cli, "_chat_payload", return_value=dict(SYNCED)):
            code = run(
                ["--config", str(self.root / "config.json"), *arguments, "--json"],
                dependencies=CliDependencies(
                    config_loader=lambda _path: loaded,
                    process_probe=mock.Mock(running=lambda **_kwargs: ()),
                    launcher=mock.Mock(),
                    artifact_warnings=artifact_warnings,
                ),
                stdout=output,
                stderr=io.StringIO(),
            )
        return code, json.loads(output.getvalue()), calls

    def test_auto_and_switch_run_the_step_with_claude_closed_and_report_it(self):
        loaded = config(self.root, stop_artifact_warnings=True)
        done = {"state": "done", "switch": "added", "records": 3}
        for arguments in (("auto",), ("switch", "Work")):
            with self.subTest(arguments=arguments):
                code, payload, calls = self.command(
                    loaded, *arguments, step=lambda _config, _running: dict(done)
                )
                self.assertEqual(0, code)
                self.assertEqual([False], calls)
                self.assertEqual(done, payload["artifact_warnings"])
                self.assertEqual("finished", payload["progress"])

    def test_a_step_with_nothing_to_do_is_not_reported(self):
        loaded = config(self.root, stop_artifact_warnings=True)
        code, payload, _calls = self.command(
            loaded, "auto", step=lambda _config, _running: {"state": "noop"}
        )
        self.assertEqual(0, code)
        self.assertNotIn("artifact_warnings", payload)

    def test_status_shows_the_option_and_what_the_last_sync_cleared(self):
        for enabled, expected in ((False, ("off", 0)), (True, ("on", 2))):
            with self.subTest(enabled=enabled):
                loaded = config(self.root, stop_artifact_warnings=enabled)
                state = loaded.state_dir
                state.mkdir(exist_ok=True)
                (state / "artifact-warnings-status.json").write_text(
                    json.dumps({"state": "done", "records": 2})
                )
                output = io.StringIO()
                run(
                    ["--config", str(self.root / "config.json"), "status", "--json"],
                    dependencies=CliDependencies(
                        config_loader=lambda _path: loaded,
                        process_probe=mock.Mock(running=lambda **_kwargs: ()),
                    ),
                    stdout=output,
                    stderr=io.StringIO(),
                )
                payload = json.loads(output.getvalue())
                self.assertEqual(
                    expected,
                    (
                        payload["artifact_warnings"],
                        payload["counts"]["artifact_warnings_cleared"],
                    ),
                )


if __name__ == "__main__":
    unittest.main()
