"""Read the archive probe's trees as a test sees them, through the store helper.

archive_probe_test (BUILD.bazel) runs this under archive_test, which mounts
each declared probe archive under ARCHIVE_MOUNT_ROOT, so it fails when a test
can no longer read a declared tree on a worker class. Run as a test, it also
checks the start markers the test launcher exports for a test that keeps its
own deadline.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

EXPECTED = b"mounted\n"
PAYLOAD = "archive probe payload\n"


def start_marker_problem(environment: Mapping[str, str]) -> str | None:
    """What is wrong with the launcher's start markers, or None."""
    if not environment.get("ARCHIVE_TEST_STARTED", "").isdigit():
        return "the launcher exported no ARCHIVE_TEST_STARTED epoch second"
    boottime = environment.get("ARCHIVE_TEST_STARTED_BOOTTIME")
    if sys.platform == "linux":
        now = time.clock_gettime(time.CLOCK_BOOTTIME)
        if boottime is None or not 0 < float(boottime) <= now:
            return f"ARCHIVE_TEST_STARTED_BOOTTIME {boottime!r} is not a start before {now}"
    elif boottime is not None:
        return f"ARCHIVE_TEST_STARTED_BOOTTIME {boottime!r} set without /proc/uptime"
    return None


def main(mounts: list[str]) -> int:
    # Reading nothing would pass whatever the mounts held.
    if not mounts:
        print("probe_reader: name the mounts to read", file=sys.stderr)
        return 2
    # `bazel run` (archive_probe_binary) starts no test clock, so it gets no
    # markers and has no TEST_TMPDIR.
    if "TEST_TMPDIR" in os.environ:
        problem = start_marker_problem(os.environ)
        if problem:
            print(f"probe_reader: {problem}", file=sys.stderr)
            return 1
    root = Path(os.environ["ARCHIVE_MOUNT_ROOT"])
    if "TEST_TMPDIR" not in os.environ:
        print(f"default_tmp={tempfile.gettempdir()}")
        print(f"mounts={','.join(sorted(entry.name for entry in root.iterdir()))}")
    for mount in mounts:
        tree = root / mount
        if "TEST_TMPDIR" not in os.environ:
            with tempfile.TemporaryDirectory(
                dir=os.environ["ARCHIVE_CLONE_SCRATCH"]
            ) as scratch:
                print(f"scratch={Path(scratch).resolve()}")
            print(f"physical={tree.resolve(strict=True)}")
        elif (
            os.environ.get("PROBE_SHELL_EXPANSION")
            and not os.environ.get("RULES_ARCHIVE_STORE_DIR")
            and not tree.resolve(strict=True).is_relative_to(
                Path(os.environ["TEST_TMPDIR"]).resolve()
            )
        ):
            print(
                f"private mount outside test volume: {tree.resolve()}", file=sys.stderr
            )
            return 1
        answer = subprocess.run(
            [str(tree / "bin/answer")], capture_output=True, check=True
        ).stdout
        payload = (tree / "share/payload.txt").read_text(encoding="ascii")
        if answer != EXPECTED or payload != PAYLOAD:
            print(
                f"probe_reader: {tree} read {answer!r} and {payload!r}", file=sys.stderr
            )
            return 1
    print(f"probe_reader: read {', '.join(mounts)} under {root}")
    if parent := os.environ.get("PROBE_SEAL_LAUNCH_PARENT"):
        Path(parent).chmod(0o500)
        # Removing the private store also needs write permission on this
        # parent, which is distinct from the launcher's output directory.
        Path(os.environ["ARCHIVE_CLONE_SCRATCH"]).parent.chmod(0o500)
    if os.environ.get("PROBE_LEAVE_SEALED_SCRATCH"):
        sealed = Path(tempfile.mkdtemp(dir=os.environ["ARCHIVE_CLONE_SCRATCH"]))
        (sealed / "payload").write_bytes(b"private clone")
        sealed.chmod(0o555)
        return 23
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
