"""Run the independent consumer against a prepared release archive.

The consumer builds this checkout through local_path_override; here it builds
the archive's extracted contents instead, so a file the archive leaves out or a
label that resolves only inside this repository fails before release.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bazel", default="bazel")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--strip-prefix", required=True)
    args = parser.parse_args()
    consumer = Path(__file__).resolve().parents[1] / "examples" / "consumer"
    with tempfile.TemporaryDirectory() as scratch:
        with tarfile.open(args.archive) as archive:
            archive.extractall(scratch, filter="data")
        module = Path(scratch) / args.strip_prefix
        # A fresh output base, so nothing built from the checkout is reused.
        output_base = Path(scratch) / "output-base"
        command = [
            args.bazel,
            "--ignore_all_rc_files",
            f"--output_base={output_base}",
        ]
        try:
            subprocess.run(
                [
                    *command,
                    "test",
                    "//...",
                    f"--override_module=curlewlabs_rules_archive_store={module}",
                    "--lockfile_mode=off",
                ],
                cwd=consumer,
                check=True,
            )
        finally:
            subprocess.run([*command, "shutdown"], cwd=consumer, check=False)
            # Bazel leaves its output base read-only.
            subprocess.run(["chmod", "-R", "u+w", str(output_base)], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
