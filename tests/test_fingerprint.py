import json
import unittest

from claude_session_sync.fingerprint import VOLATILE_KEYS, fingerprint

SID = "11111111-1111-4111-8111-111111111111"


def record(**fields):
    body = {"sessionId": "local_" + SID, "title": "t", "lastActivityAt": 100}
    body.update(fields)
    return json.dumps(body).encode()


class FingerprintTests(unittest.TestCase):
    def test_what_claude_rewrites_without_user_action_is_not_state(self):
        before = fingerprint(SID, record(error="never started", errorAt=111))
        after = fingerprint(SID, record(error="never started", errorAt=222, lastFocusedAt=999))

        self.assertEqual(before.state_hash, after.state_hash)

    def test_each_accounts_connector_ids_are_not_state(self):
        # Measured on Claude 2.2553.13: the other account rewrote enabledMcpTools
        # with its own connector ids in 100 chats without any chat activity.
        one = fingerprint(
            SID,
            record(
                remoteMcpServersConfig=[{"uuid": "u1"}],
                enabledMcpTools={"mcp__u1__search": True},
            ),
        )
        other = fingerprint(
            SID,
            record(
                remoteMcpServersConfig=[{"uuid": "u9"}],
                enabledMcpTools={"mcp__u9__search": False},
            ),
        )

        self.assertEqual(one.state_hash, other.state_hash)
        self.assertEqual(one.state_hash, fingerprint(SID, record()).state_hash)

    def test_runtime_snapshots_and_disk_checks_are_not_state(self):
        plain = fingerprint(SID, record())
        for key in (
            "transcriptUnavailable",
            "promptSuggestion",
            "promptAppendSnapshot",
            "toolSurfaceSnapshot",
        ):
            with self.subTest(key=key):
                self.assertIn(key, VOLATILE_KEYS)
                self.assertEqual(
                    plain.state_hash, fingerprint(SID, record(**{key: "x"})).state_hash
                )

    def test_a_string_cut_through_an_emoji_is_still_a_readable_record(self):
        cut = b'{"sessionId": "local_%s", "report": "done \\ud83d", "lastActivityAt": 7}' % SID.encode()
        whole = b'{"sessionId": "local_%s", "report": "done", "lastActivityAt": 7}' % SID.encode()

        self.assertTrue(fingerprint(SID, cut).readable)
        self.assertEqual(fingerprint(SID, cut).last_activity_at, 7)
        self.assertNotEqual(fingerprint(SID, cut).state_hash, fingerprint(SID, whole).state_hash)

    def test_key_order_and_spacing_do_not_change_the_state(self):
        one = fingerprint(SID, b'{"sessionId":"local_%s","title":"t"}' % SID.encode())
        two = fingerprint(SID, b'{ "title": "t",\n "sessionId": "local_%s" }' % SID.encode())

        self.assertEqual(one.state_hash, two.state_hash)

    def test_any_other_field_changes_the_state(self):
        for change in (
            {"title": "renamed"},
            {"isArchived": True},
            {"cliSessionId": "c2"},
            {"lastActivityAt": 101},
            {"prState": "MERGED"},
        ):
            with self.subTest(change=change):
                self.assertNotEqual(
                    fingerprint(SID, record()).state_hash,
                    fingerprint(SID, record(**change)).state_hash,
                )

    def test_a_volatile_key_nested_deeper_is_still_state(self):
        plain = fingerprint(SID, record(spawnSeed={}))
        nested = fingerprint(SID, record(spawnSeed={"lastFocusedAt": 1}))

        self.assertNotEqual(plain.state_hash, nested.state_hash)

    def test_activity_is_read_and_floored(self):
        self.assertEqual(fingerprint(SID, record(lastActivityAt=1234)).last_activity_at, 1234)
        self.assertEqual(fingerprint(SID, record(lastActivityAt=1234.9)).last_activity_at, 1234)

    def test_missing_or_odd_activity_reads_as_zero(self):
        for value in (None, "soon", True, -3, -0.5):
            with self.subTest(value=value):
                self.assertEqual(fingerprint(SID, record(lastActivityAt=value)).last_activity_at, 0)

    def test_what_claude_would_not_accept_is_unreadable(self):
        cases = {
            "torn write": b'{"sessionId": "local_',
            "empty file": b"",
            "not an object": b"[1, 2]",
            "another session": record(sessionId="local_22222222-2222-4222-8222-222222222222"),
            "no session id": b'{"title": "t"}',
            "not utf-8": b"\xff\xfe{}",
            "duplicate keys": b'{"sessionId": "local_%s", "a": 1, "a": 2}' % SID.encode(),
            "non-finite number": b'{"sessionId": "local_%s", "a": NaN}' % SID.encode(),
        }
        for name, data in cases.items():
            with self.subTest(case=name):
                self.assertFalse(fingerprint(SID, data).readable)


if __name__ == "__main__":
    unittest.main()
