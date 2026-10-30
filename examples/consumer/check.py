"""Read each named tree under ARCHIVE_MOUNT_ROOT as the archive holds it."""

import os
import subprocess
import sys
from pathlib import Path


def main(trees: list[str]) -> int:
    if not trees:
        print("check: name the trees to read", file=sys.stderr)
        return 2
    root = Path(os.environ["ARCHIVE_MOUNT_ROOT"])
    for tree in trees:
        said = subprocess.run(
            [str(root / tree / "bin/hello")], capture_output=True, check=True
        ).stdout
        data = (root / tree / "share/data.txt").read_bytes()
        if said != b"hello from the archive\n" or data != b"data\n":
            print(f"check: {tree} read {said!r} and {data!r}", file=sys.stderr)
            return 1
    print(f"check: read {', '.join(trees)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
