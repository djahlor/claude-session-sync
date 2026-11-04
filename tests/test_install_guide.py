"""INSTALL.md is what Claude Code follows. Keep it in step with the real installer."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from claude_session_sync.installer import InstallLayout

ROOT = Path(__file__).resolve().parents[1]
GUIDE = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
INSTALL_SCRIPT = (ROOT / "install.sh").read_text(encoding="utf-8")
GET_SCRIPT = (ROOT / "get.sh").read_text(encoding="utf-8")


def _quoted_requests(text: str) -> list[str]:
    """The sentences a person pastes into Claude Code, from text blocks and quotes."""

    blocks = re.findall(r"```text\n(.+?)\n```", text)
    quotes = re.findall(r"^> (.+)$", text, flags=re.MULTILINE)
    return [line for line in blocks + quotes if "github.com/djahlor" in line]


class InstallGuideTests(unittest.TestCase):
    def test_the_readme_sentences_are_the_ones_the_guide_expects(self) -> None:
        readme = _quoted_requests(README)
        guide = _quoted_requests(GUIDE)
        self.assertEqual(2, len(readme), readme)
        self.assertEqual(readme, guide)
        self.assertTrue(readme[0].startswith("Install Claude Session Sync"))
        self.assertTrue(readme[1].startswith("Remove Claude Session Sync"))

    def test_every_option_in_the_guide_is_one_install_sh_takes(self) -> None:
        accepted = set(re.findall(r"^    (--[a-z-]+)\)$", INSTALL_SCRIPT, re.M))
        install = GUIDE[GUIDE.index("## 6. Install") : GUIDE.index("## 7.")]
        command = install.split("```sh")[1].split("```")[0]
        named = set(re.findall(r"`(--[a-z][a-z-]*)`", install))
        named |= set(re.findall(r"(--[a-z][a-z-]*)", command))
        self.assertTrue(named)
        self.assertEqual(set(), named - accepted)

    def test_both_routes_install_the_same_way(self) -> None:
        guide = re.search(r"\./install\.sh ((?:--[a-z-]+ ?)+)", GUIDE)
        terminal = re.search(r'install\.sh" ((?:--[a-z-]+ ?)+)', GET_SCRIPT)
        self.assertIsNotNone(guide)
        self.assertIsNotNone(terminal)
        self.assertEqual(guide.group(1).split(), terminal.group(1).split())

    def test_the_network_check_finds_nothing_in_the_code(self) -> None:
        pattern = re.search(r'grep -rnE "([^"]+)"', GUIDE).group(1)
        expression = re.compile(pattern)
        sources = [ROOT / "install.sh"] + [
            path
            for path in (ROOT / "src").rglob("*")
            if path.is_file() and path.suffix in (".py", ".swift", ".cc", ".h")
        ]
        hits = [
            f"{path.relative_to(ROOT)}:{number}"
            for path in sources
            for number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            )
            if expression.search(line)
        ]
        self.assertEqual([], hits)

    def test_the_paths_the_guide_names_are_the_real_ones(self) -> None:
        home = Path("/home-of-test")
        layout = InstallLayout.for_home(home)
        for path in (
            layout.support_dir,
            layout.config_path.parent,
            layout.launch_agent,
            layout.runtime_cli,
        ):
            with self.subTest(path=path):
                self.assertIn("~/" + str(path.relative_to(home)), GUIDE + _home(GUIDE))
        label = layout.launch_agent.stem
        self.assertIn(f'launchctl print "gui/$(id -u)/{label}"', GUIDE)


def _home(text: str) -> str:
    """The guide's $HOME paths written with a tilde, to compare them the same way."""

    return text.replace('"$HOME/', "~/")


if __name__ == "__main__":
    unittest.main()
