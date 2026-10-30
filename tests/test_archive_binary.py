"""archive_binary's launcher must leave nothing behind and keep the binary's answer.

Under `bazel run` there is no test directory to hold the mounts, so the launcher
makes a root of its own; a root left behind would pile up in the caller's
temporary directory with every run, and a lost exit status would let a failed
run, such as the Android lock refresh, pass for a good one.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from typing import override

INFINITE: float = import_module("unittest_timeouts").INFINITE

_LAUNCHER = Path(__file__).with_name("archive_probe_binary.archive_launcher.sh")


class ArchiveBinaryTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.temporary = Path(tempfile.mkdtemp(dir=os.environ.get("TEST_TMPDIR")))

    def _run(
        self, *arguments: str, extra: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            name: value
            for name, value in os.environ.items()
            # What `bazel run` lacks, and what would point the helper at a
            # store or mount root other than the launcher's own.
            if name
            not in {
                "TEST_TMPDIR",
                "ARCHIVE_MOUNT_ROOT",
                "RULES_ARCHIVE_STORE_DIR",
                "RUNNER_TEMP",
            }
        }
        environment["TMPDIR"] = str(self.temporary)
        environment.update(extra or {})
        return subprocess.run(
            [str(_LAUNCHER), *arguments],
            capture_output=True,
            check=False,
            env=environment,
            text=True,
            timeout=INFINITE,
        )

    def test_mounts_and_forwards_arguments_then_removes_its_root(self) -> None:
        result = self._run("tar", "zip")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("probe_reader: read tar, zip under", result.stdout)
        # The mount root and the helper's private store were made here.
        self.assertEqual(list(self.temporary.iterdir()), [])

    def test_keeps_a_failed_binarys_status_and_still_removes_its_root(self) -> None:
        # probe_reader refuses to read no mounts, exiting 2.
        result = self._run()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(list(self.temporary.iterdir()), [])

    def test_an_actions_job_expands_into_its_own_temporary_directory(self) -> None:
        # A Linux runner's sweep deletes /tmp entries by mtime, and an
        # expansion's entries carry the archive's zero mtimes, so a store left
        # in TMPDIR could lose its tree mid-run; RUNNER_TEMP is outside any age
        # rule.
        job = Path(tempfile.mkdtemp(dir=os.environ.get("TEST_TMPDIR")))
        result = self._run("tar", "zip", extra={"RUNNER_TEMP": str(job)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            f"expanding into private store {job}/archive-store.", result.stderr
        )
        self.assertEqual(list(self.temporary.iterdir()), [])
        self.assertEqual(list(job.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
