"""The status window from the real watcher source, opened and closed by a small harness."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).parents[1] / "src/claude_session_sync/SessionSyncWatcher.swift"

HARNESS = r"""
let application = NSApplication.shared
application.setActivationPolicy(.accessory)
private let notice = StatusNotice()
private var report: [String: Bool] = [:]
DispatchQueue.main.async {
  let first = notice.show("Account switched. Claude closes, syncs, and opens again by itself.")
  report["shown"] = first.isVisible
  report["close_button"] = first.standardWindowButton(.closeButton)?.isEnabled ?? false
  report["minimize_hidden"] = first.standardWindowButton(.miniaturizeButton)?.isHidden ?? true
  report["text_fits"] = first.contentView?.subviews.allSatisfy {
    $0.fittingSize.height <= $0.frame.height + 1
  } ?? false
  first.standardWindowButton(.closeButton)?.performClick(nil)
  report["closed_by_button"] = !first.isVisible

  let second = notice.show("Sync finished. Claude is reopening.")
  let point = NSPoint(x: 40, y: 12)
  let click = NSEvent.mouseEvent(
    with: .leftMouseDown, location: point, modifierFlags: [], timestamp: 0,
    windowNumber: second.windowNumber, context: nil, eventNumber: 0, clickCount: 1,
    pressure: 1)!
  second.contentView?.hitTest(point)?.mouseDown(with: click)
  report["closed_by_click"] = !second.isVisible

  let third = notice.show("Restarting Claude to sync.")
  let fourth = notice.show("Sync needs attention.")
  report["one_at_a_time"] = !third.isVisible && fourth.isVisible
  notice.close()
  report["closed_by_code"] = !fourth.isVisible

  let data = try! JSONSerialization.data(withJSONObject: report, options: [.sortedKeys])
  print(String(data: data, encoding: .utf8)!)
  exit(0)
}
application.run()
"""


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "macOS window")
class StatusWindowTests(unittest.TestCase):
    """The window that says Claude quits or reopens by itself can be closed."""

    def test_the_status_window_closes_by_button_click_and_code(self):
        source = SOURCE.read_text()
        types = source[: source.index("private let separatorIndex")]
        with tempfile.TemporaryDirectory(prefix="status-window-test-") as directory:
            program = Path(directory) / "main.swift"
            program.write_text(types + HARNESS)
            binary = Path(directory) / "status-window"
            subprocess.run(
                ["/usr/bin/xcrun", "swiftc", str(program), "-o", str(binary)],
                check=True,
                capture_output=True,
                timeout=120,
            )
            result = subprocess.run(
                [str(binary)], capture_output=True, text=True, timeout=30, check=True
            )
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(
            report,
            {
                "shown": True,
                "close_button": True,
                "minimize_hidden": True,
                "text_fits": True,
                "closed_by_button": True,
                "closed_by_click": True,
                "one_at_a_time": True,
                "closed_by_code": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
