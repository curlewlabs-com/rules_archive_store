"""Check the toolchain refresh against the bumps it exists to finish.

The refresh runs unattended on an updater's branch, so the cases here are the
ways a bump goes wrong without anyone watching: one pin file left on the old
release, a host archive the new release does not publish, and a README whose
wording moved out from under the rewrite.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import refresh_toolchain
from refresh_toolchain import RefreshError, refresh

OLD = "a" * 64
NEW_ARM = "b" * 64
NEW_X64 = "c" * 64

SHASUMS = f"{NEW_ARM}  bun-darwin-aarch64.zip\n{NEW_X64}  bun-linux-x64-baseline.zip\n"

README = "Developed with Bazel 9.0.0 (.bazelversion), and Bun 1.0.0 for the suite.\n"


def deps(bun: str) -> str:
    return (
        "_BUN = {\n"
        f'    "mac os x/aarch64": ("darwin-aarch64", "{OLD}"),\n'
        f'    "linux/amd64": ("linux-x64-baseline", "{OLD}"),\n'
        "}\n"
        f'urls = ["https://github.com/oven-sh/bun/releases/download/bun-v{bun}/bun-"]\n'
    )


class RefreshTest(unittest.TestCase):
    def tree(self, bun: str = "1.1.0") -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for path in refresh_toolchain.BUN_PINS:
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(deps(bun))
        for path in refresh_toolchain.BAZEL_PINS:
            (root / path).write_text("9.1.0\n")
        (root / refresh_toolchain.README).write_text(README)
        return root

    def test_writes_each_archive_its_own_digest_and_the_readme(self) -> None:
        root = self.tree()
        changed = refresh(root, SHASUMS)
        self.assertEqual(
            sorted(changed), sorted([*refresh_toolchain.BUN_PINS, "README.md"])
        )
        for path in refresh_toolchain.BUN_PINS:
            text = (root / path).read_text()
            self.assertIn(f'("darwin-aarch64", "{NEW_ARM}")', text)
            self.assertIn(f'("linux-x64-baseline", "{NEW_X64}")', text)
        readme = (root / "README.md").read_text()
        self.assertIn("Bazel 9.1.0 (.bazelversion), and Bun 1.1.0 for", readme)

    def test_a_second_run_changes_nothing(self) -> None:
        root = self.tree()
        refresh(root, SHASUMS)
        self.assertEqual(refresh(root, SHASUMS), [])

    def test_a_pin_file_left_behind_fails_and_writes_nothing(self) -> None:
        root = self.tree()
        stale = root / refresh_toolchain.BUN_PINS[1]
        stale.write_text(deps("1.0.0"))
        with self.assertRaisesRegex(RefreshError, "Bun is not pinned to one"):
            refresh(root, SHASUMS)
        self.assertIn(OLD, (root / refresh_toolchain.BUN_PINS[0]).read_text())

    def test_an_archive_the_release_lacks_fails_and_writes_nothing(self) -> None:
        root = self.tree()
        with self.assertRaisesRegex(RefreshError, "bun-linux-x64-baseline.zip"):
            refresh(root, f"{NEW_ARM}  bun-darwin-aarch64.zip\n")
        self.assertIn(OLD, (root / refresh_toolchain.BUN_PINS[0]).read_text())
        self.assertEqual((root / "README.md").read_text(), README)

    def test_a_readme_without_the_bun_version_fails(self) -> None:
        root = self.tree()
        (root / "README.md").write_text(README.replace("Bun 1.0.0", "Bun"))
        with self.assertRaisesRegex(RefreshError, "README matches"):
            refresh(root, SHASUMS)
        self.assertIn(OLD, (root / refresh_toolchain.BUN_PINS[0]).read_text())


if __name__ == "__main__":
    unittest.main()
