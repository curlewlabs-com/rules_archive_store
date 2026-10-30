"""Read the archive probe's trees as a test sees them, through the store helper.

archive_probe_test (BUILD.bazel) runs this under archive_test, which mounts
each declared probe archive under ARCHIVE_MOUNT_ROOT, so it fails when a test
can no longer read a declared tree on a worker class.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

EXPECTED = b"mounted\n"
PAYLOAD = "archive probe payload\n"


def main(mounts: list[str]) -> int:
    # Reading nothing would pass whatever the mounts held.
    if not mounts:
        print("probe_reader: name the mounts to read", file=sys.stderr)
        return 2
    root = Path(os.environ["ARCHIVE_MOUNT_ROOT"])
    for mount in mounts:
        tree = root / mount
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
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
