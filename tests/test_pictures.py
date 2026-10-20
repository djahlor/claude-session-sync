import json
import os
import tempfile
import unittest
from pathlib import Path

from claude_session_sync.config import Config, load_config
from claude_session_sync.model import Profile
from claude_session_sync.pictures import OFFSETS_FILENAME, PictureFixer

UUID = "e19127e8-56ed-4c5c-b2a4-8ec50f0c83cd"
OTHER = "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"
SESSION = "7b7752b3-7c7b-4709-8b61-746cf1cb6248"


def config(root):
    return Config(
        profiles=(Profile("Work", root / "Claude", ("/usr/bin/true",), True),),
        state_dir=root / "state",
        retention=5,
        acknowledge_cross_profile_copy=False,
        acknowledge_cross_account_copy=True,
        claude_executable=Path("/usr/bin/true"),
    )


def sent(path, uuid=UUID, *, size=None, is_image=True, extra=None):
    """One transcript line as Claude writes it after SendUserFile."""

    attachment = {"path": str(path), "size": size, "isImage": is_image, "media_type": "image/png",
                  "pathValidated": True, "file_uuid": uuid}
    if size is None:
        del attachment["size"]
    line = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content":
                    "1 file delivered to user.\n  {} → file_uuid: {}".format(path, uuid)}]},
        "toolUseResult": {"caption": "x", "attachments": [attachment], "rendered_locally": True},
    }
    line.update(extra or {})
    return (json.dumps(line, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


class PictureFixerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.projects = self.root / "projects" / "-Users-me-repo"
        self.projects.mkdir(parents=True)
        self.picture = self.root / "photo.png"
        self.picture.write_bytes(b"\x89PNG" + b"0" * 96)
        self.record(SESSION)
        self.fixer = PictureFixer(config(self.root), root=self.root / "projects")

    def record(self, session_id, account="acct/org"):
        folder = self.root / "Claude" / "claude-code-sessions" / account
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "local_{}.json".format(session_id[:8])).write_text(
            json.dumps({"sessionId": "local_x", "cliSessionId": session_id})
        )

    def tearDown(self):
        self.temp.cleanup()

    def transcript(self, *lines, session_id=None):
        path = self.projects / "{}.jsonl".format(session_id or SESSION)
        path.write_bytes(b"".join(lines))
        return path

    def test_local_picture_drops_its_upload_id_in_place(self):
        before = sent(self.picture, size=100)
        path = self.transcript(b'{"type":"summary"}\n', before, b'{"type":"assistant"}\n')
        size = path.stat().st_size

        receipt = self.fixer.sync()

        content = path.read_bytes()
        self.assertEqual(size, len(content))
        line = content.splitlines(keepends=True)[1]
        record = json.loads(line)["toolUseResult"]["attachments"][0]
        self.assertNotIn("file_uuid", record)
        self.assertEqual(UUID, record["orig_uuid"])
        # The model's own text keeps the id it was given.
        self.assertIn("file_uuid: " + UUID, json.loads(line)["message"]["content"][0]["content"])
        self.assertEqual(before.replace(b'"file_uuid":"', b'"orig_uuid":"'), line)
        self.assertEqual(("synced", 1, 1), (receipt.state, receipt.file_count, receipt.picture_count))

    def test_pictures_that_would_not_show_are_left_alone(self):
        missing = self.root / "gone.png"
        other = self.root / "notes.pdf"
        other.write_bytes(b"%PDF")
        lines = [
            sent(missing),
            sent(other, is_image=False),
            sent(self.picture, size=7),
            sent(self.picture, uuid="not-a-uuid"),
            sent(self.root / "Claude" / "inside.png"),
            sent("relative.png"),
        ]
        (self.root / "Claude").mkdir(exist_ok=True)
        (self.root / "Claude" / "inside.png").write_bytes(b"x")
        path = self.transcript(*lines)
        before = path.read_bytes()

        self.assertEqual("noop", self.fixer.sync().state)
        self.assertEqual(before, path.read_bytes())

    def test_only_synced_chats_are_read(self):
        stranger = "11111111-2222-3333-4444-555555555555"
        path = self.transcript(sent(self.picture), session_id=stranger)
        before = path.read_bytes()
        self.assertEqual("noop", self.fixer.sync().state)
        self.assertEqual(before, path.read_bytes())
        self.record(stranger, account="other/org")
        self.assertEqual(1, self.fixer.sync().picture_count)

    def test_symlinked_picture_is_left_alone(self):
        link = self.root / "link.png"
        os.symlink(self.picture, link)
        path = self.transcript(sent(link))
        before = path.read_bytes()
        self.fixer.sync()
        self.assertEqual(before, path.read_bytes())

    def test_a_line_still_being_written_waits(self):
        line = sent(self.picture)
        path = self.transcript(line[:-1])
        self.fixer.sync()
        self.assertIn(b'"file_uuid":"', path.read_bytes())
        with open(path, "ab") as stream:
            stream.write(b"\n")
        self.fixer.sync()
        self.assertNotIn(b'"file_uuid":"', path.read_bytes())

    def test_lines_are_read_once_and_appends_are_picked_up(self):
        path = self.transcript(sent(self.picture))
        self.fixer.sync()
        offsets = json.loads((self.root / "state" / OFFSETS_FILENAME).read_text())
        self.assertEqual(path.stat().st_size, offsets["files"][str(path)]["scanned"])

        with open(path, "ab") as stream:
            stream.write(sent(self.picture, uuid=OTHER))
        receipt = self.fixer.sync()
        self.assertEqual(1, receipt.picture_count)
        self.assertNotIn(b'"file_uuid":"', path.read_bytes())

    def test_a_replaced_transcript_is_scanned_from_the_start(self):
        path = self.transcript(b'{"type":"summary"}\n' * 3)
        self.fixer.sync()
        replacement = path.with_suffix(".new")
        replacement.write_bytes(sent(self.picture))
        os.replace(replacement, path)
        self.assertEqual(1, self.fixer.sync().picture_count)

    def test_malformed_and_unrelated_lines_are_kept(self):
        broken = b'{"toolUseResult":{"attachments":[{"file_uuid":"' + UUID.encode() + b'"\n'
        path = self.transcript(broken, b"not json\n", sent(self.picture))
        self.fixer.sync()
        lines = path.read_bytes().splitlines(keepends=True)
        self.assertEqual(broken, lines[0])
        self.assertEqual(b"not json\n", lines[1])
        self.assertIn(b'"orig_uuid":"', lines[2])

    def test_a_second_run_changes_nothing(self):
        path = self.transcript(sent(self.picture))
        self.fixer.sync()
        once = path.read_bytes()
        (self.root / "state" / OFFSETS_FILENAME).unlink()
        self.assertEqual("noop", self.fixer.sync().state)
        self.assertEqual(once, path.read_bytes())

    def test_unknown_offsets_file_stops_the_run(self):
        from claude_session_sync.pictures import PictureError

        self.transcript(sent(self.picture))
        (self.root / "state").mkdir()
        (self.root / "state" / OFFSETS_FILENAME).write_text('{"version": 9}')
        with self.assertRaises(PictureError):
            self.fixer.sync()

    def test_config_turns_the_fix_on_by_default(self):
        document = {
            "version": 1,
            "profiles": [{"name": "Work", "data_root": str(self.root / "Claude"),
                          "launch_command": ["/usr/bin/true"], "enabled": True, "is_default": True}],
            "approved_targets": [],
            "state_dir": str(self.root / "state"),
            "retention": 5,
            "acknowledge_cross_profile_copy": False,
            "acknowledge_cross_account_copy": True,
            "claude_executable": "/usr/bin/true",
        }
        path = self.root / "config.json"
        path.write_text(json.dumps(document))
        self.assertTrue(load_config(path).fix_chat_pictures)
        document["fix_chat_pictures"] = False
        path.write_text(json.dumps(document))
        self.assertFalse(load_config(path).fix_chat_pictures)


if __name__ == "__main__":
    unittest.main()


class SyncStepTests(unittest.TestCase):
    """The picture step runs after the chat sync and speaks only when it acts or fails."""

    def run_step(self, fixer):
        from types import SimpleNamespace

        from claude_session_sync.cli import _fix_chat_pictures

        with tempfile.TemporaryDirectory() as directory:
            payload = {"state": "committed"}
            deps = SimpleNamespace(picture_factory=lambda _config: fixer)
            _fix_chat_pictures(config(Path(directory)), deps, payload)
            status = json.loads((Path(directory) / "state" / "chat-pictures-status.json").read_text())
            return payload, status

    def test_quiet_when_nothing_changed(self):
        from claude_session_sync.pictures import PictureReceipt

        fixer = type("F", (), {"sync": lambda self: PictureReceipt("noop", 0, 0)})()
        payload, status = self.run_step(fixer)
        self.assertEqual({"state": "committed"}, payload)
        self.assertEqual("noop", status["state"])

    def test_reports_pictures_it_fixed(self):
        from claude_session_sync.pictures import PictureReceipt

        fixer = type("F", (), {"sync": lambda self: PictureReceipt("synced", 2, 5)})()
        payload, _status = self.run_step(fixer)
        self.assertEqual({"state": "synced", "files": 2, "pictures": 5}, payload["pictures"])

    def test_a_failure_is_reported_and_the_sync_goes_on(self):
        def boom(_self):
            raise OSError("disk")

        fixer = type("F", (), {"sync": boom})()
        payload, status = self.run_step(fixer)
        self.assertEqual("committed", payload["state"])
        self.assertEqual("pictures-error", payload["pictures"]["reason"])
        self.assertEqual("skipped", status["state"])
