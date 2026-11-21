"""archive_binary's launcher must leave nothing behind and keep the binary's answer.

Under `bazel run` there is no test directory to hold the mounts, so the launcher
makes a root of its own; a root left behind would pile up in the caller's
temporary directory with every run, and a lost exit status would let a failed
run, such as the Android lock refresh, pass for a good one.
"""

from __future__ import annotations

import os
import shutil
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

    def test_a_local_run_expands_and_stages_under_bazel_outputs(self) -> None:
        # System temporary storage can be a tmpfs with no file cloning.
        output = self.temporary / "bazel-out"
        output.mkdir()
        ordinary = self.temporary / "ordinary"
        ordinary.mkdir()
        result = self._run(
            "tar",
            extra={"TMPDIR": str(ordinary), "BUILD_EXECROOT": str(self.temporary)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mounts=tar,zip", result.stdout)
        default_tmp = next(
            line.removeprefix("default_tmp=")
            for line in result.stdout.splitlines()
            if line.startswith("default_tmp=")
        )
        self.assertEqual(Path(default_tmp).resolve(), ordinary.resolve())
        locations = [
            line.split("=", 1)[1]
            for line in result.stdout.splitlines()
            if line.startswith(("scratch=", "physical="))
        ]
        self.assertEqual(len(locations), 2, result.stdout)
        for path in locations:
            self.assertTrue(Path(path).is_relative_to(output.resolve()), path)
        self.assertEqual(list(output.iterdir()), [])

    def test_relative_scratch_parent_survives_a_child_directory_change(self) -> None:
        # The system temporary parent can be relative to the runfiles tree;
        # the clone destination the command receives must survive a chdir.
        parent = str(
            Path("tests") / os.path.relpath(self.temporary, Path("tests").resolve())
        )
        result = self._run(
            "tar",
            extra={
                "RUNNER_TEMP": parent,
                "CDPATH": ".",
                "PROBE_CHANGE_DIRECTORY": str(self.temporary),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.temporary.iterdir()), [])

    def test_failed_run_reclaims_a_sealed_clone_from_child_scratch(self) -> None:
        output = self.temporary / "bazel-out"
        output.mkdir()
        result = self._run(
            "tar",
            extra={
                "TMPDIR": "",
                "BUILD_EXECROOT": str(self.temporary),
                "PROBE_LEAVE_SEALED_SCRATCH": "1",
            },
        )
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(list(output.iterdir()), [])

    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory permissions")
    def test_cleanup_failure_preserves_a_prior_command_failure(self) -> None:
        # Prevent the launcher from removing its own root only after the child
        # has started. Both errors are real and the first failure must survive.
        output = self.temporary / "bazel-out"
        for child_fails, expected in (("", 1), ("1", 23)):
            with self.subTest(child_fails=child_fails):
                output.mkdir()
                try:
                    result = self._run(
                        "tar",
                        extra={
                            "TMPDIR": "",
                            "BUILD_EXECROOT": str(self.temporary),
                            "PROBE_SEAL_LAUNCH_PARENT": str(output),
                            "PROBE_LEAVE_SEALED_SCRATCH": child_fails,
                        },
                    )
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertTrue(list(output.iterdir()))
                finally:
                    output.chmod(0o700)
                    for run_root in output.iterdir():
                        run_root.chmod(0o700)
                    shutil.rmtree(output)

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
            f"expanding into private store {job}/archive-mounts.", result.stderr
        )
        self.assertEqual(list(self.temporary.iterdir()), [])
        self.assertEqual(list(job.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
