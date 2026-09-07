import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from claude_session_sync.install_transaction import (
    InstallRecoveryError,
    InstallTransaction,
)
from claude_session_sync.locking import LockUnavailableError


class UnloadedRunner:
    def __call__(self, command, **kwargs):
        return subprocess.CompletedProcess(command, 113, stdout="", stderr="")


class DeniedRunner:
    def __call__(self, command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="denied")


class InstallTransactionTests(unittest.TestCase):
    def test_uncaught_process_exit_is_recovered_by_the_next_installer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "installed"
            target.write_bytes(b"before")
            program = (
                "import os, subprocess, sys\n"
                "from pathlib import Path\n"
                "from claude_session_sync.install_transaction import InstallTransaction\n"
                "root=Path(sys.argv[1]); target=root/'installed'\n"
                "runner=lambda command, **kw: subprocess.CompletedProcess(command, 113, '', '')\n"
                "tx=InstallTransaction(targets=(target,), state_roots=(root/'state',), "
                "launch_agent=root/'watcher.plist', recovery_root=root/'install-state', runner=runner)\n"
                "with tx:\n"
                " target.write_bytes(b'partial')\n"
                " os._exit(23)\n"
            )
            result = subprocess.run([sys.executable, "-c", program, str(root)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertEqual(target.read_bytes(), b"partial")
            with self.transaction(root, target):
                self.assertEqual(target.read_bytes(), b"before")
            self.assertFalse((root / "install-state").exists())

    def test_second_installer_cannot_overlap_the_first(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "installed"
            target.write_bytes(b"before")
            with self.transaction(root, target):
                with self.assertRaises(LockUnavailableError):
                    self.transaction(root, target).__enter__()
                self.assertEqual(target.read_bytes(), b"before")

    def test_symlinked_recovery_root_is_rejected_before_any_recovery_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "installed"
            target.write_bytes(b"before")
            outside = root / "outside"
            outside.mkdir()
            (outside / "manifest.json").write_text("{}")
            (root / "install-state").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink ancestor"):
                self.transaction(root, target).__enter__()
            self.assertEqual(target.read_bytes(), b"before")
            self.assertTrue((outside / "manifest.json").exists())
            self.assertFalse((root / "state").exists())

    def test_symlinked_state_root_parent_is_rejected_before_lock_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "installed"
            target.write_bytes(b"before")
            outside = root / "outside"
            outside.mkdir()
            parent = root / "linked"
            parent.symlink_to(outside, target_is_directory=True)
            transaction = self.transaction(root, target)
            transaction.state_roots = (parent / "state",)
            with self.assertRaisesRegex(ValueError, "symlink ancestor.*linked"):
                transaction.__enter__()
            self.assertEqual(list(outside.iterdir()), [])
            self.assertFalse((root / "install-state").exists())

    def transaction(self, root, target):
        return InstallTransaction(
            targets=(target,),
            state_roots=(root / "state",),
            launch_agent=root / "watcher.plist",
            recovery_root=root / "install-state",
            runner=UnloadedRunner(),
        )

    def test_next_transaction_recovers_durable_interrupted_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "installed"
            target.write_bytes(b"before")
            interrupted = self.transaction(root, target)
            interrupted.__enter__()
            target.write_bytes(b"partial")
            interrupted._release()  # Simulate process death after durable prepare.

            with self.transaction(root, target):
                self.assertEqual(target.read_bytes(), b"before")

            self.assertFalse((root / "install-state").exists())

            target.write_bytes(b"later-edit")
            with self.transaction(root, target):
                self.assertEqual(target.read_bytes(), b"later-edit")

    def test_corrupt_recovery_is_retained_for_manual_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "installed"
            target.write_bytes(b"before")
            interrupted = self.transaction(root, target)
            interrupted.__enter__()
            (root / "install-state" / "snapshot" / "0").write_bytes(b"corrupt")
            interrupted._release()

            with self.assertRaisesRegex(InstallRecoveryError, "evidence retained"):
                self.transaction(root, target).__enter__()

            self.assertTrue((root / "install-state" / "manifest.json").exists())

    def test_manifestless_published_recovery_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "installed"
            target.write_bytes(b"before")
            (root / "install-state").mkdir()

            with self.assertRaisesRegex(InstallRecoveryError, "no manifest"):
                self.transaction(root, target).__enter__()

            self.assertTrue((root / "install-state").exists())

    def test_symlink_target_is_rejected_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.write_bytes(b"outside")
            target = root / "installed"
            target.symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "symlink ancestor"):
                self.transaction(root, target).__enter__()

            self.assertFalse((root / "install-state").exists())
            self.assertEqual(outside.read_bytes(), b"outside")

    def test_symlinked_target_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_parent = root / "real-parent"
            real_parent.mkdir()
            linked_parent = root / "linked-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symlink ancestor"):
                self.transaction(root, linked_parent / "installed").__enter__()

            self.assertFalse((real_parent / "installed").exists())

    def test_directory_root_mode_changes_snapshot_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tree"
            root.mkdir(mode=0o700)
            first = InstallTransaction._digest(root)
            root.chmod(0o755)
            self.assertNotEqual(first, InstallTransaction._digest(root))

    def test_unexpected_launchctl_print_error_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "installed"
            transaction = InstallTransaction(
                targets=(target,), state_roots=(root / "state",),
                launch_agent=root / "watcher.plist",
                recovery_root=root / "install-state", runner=DeniedRunner(),
            )
            with self.assertRaisesRegex(RuntimeError, "could not verify"):
                transaction.__enter__()


if __name__ == "__main__":
    unittest.main()
