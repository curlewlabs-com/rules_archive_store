"""Check archive_clone's types as a caller's checker reads them where it is installed.

The repository's own mypy run reads archive_clone.py as source, so it says
nothing about what a caller sees. A caller reaches the module through an
installed search path, a site-packages `.pth` line naming the directory as a
Bazel runfiles tree is put on a checker's path, and there PEP 561 applies: an
unmarked single-file module reads as untyped. This builds that search path in
a scratch venv, then runs two checks under it:

- mypy strict over a caller of `clone` and `copy`, which fails if the stub
  package is not found, or if a call does not match it;
- mypy's stubtest, which fails if the stub and the module disagree, so the
  stub cannot drift from the signatures it states.

The venv's `.pth` also names the running interpreter's site-packages, where
requirements-dev.txt installs mypy, so it needs nothing installed of its own.
"""

from __future__ import annotations

import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

STORE = Path(__file__).resolve().parents[1] / "archive_store"

CALLER = """\
from pathlib import Path

import archive_clone

made: int = archive_clone.clone(
    Path("mount"),
    Path("work"),
    writable_files=True,
    writable_directories=False,
    materialize_file_links=True,
)
copied: int = archive_clone.copy(Path("mount"), Path("work"), writable_files=True)
"""


def _purelib(python: Path) -> Path:
    found = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(found.stdout.strip())


def main() -> int:
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        venv = root / "venv"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True
        )
        python = venv / "bin" / "python"
        # The checker's own install, so mypy runs under the venv, then the
        # store's directory as a caller's search path names it.
        tools = sysconfig.get_path("purelib")
        lines = f"{tools}\n{STORE}\n"
        (_purelib(python) / "archive_store.pth").write_text(lines, encoding="utf-8")
        caller = root / "caller.py"
        caller.write_text(CALLER, encoding="utf-8")
        checks = {
            "mypy --strict over a caller": ["-m", "mypy", "--strict", str(caller)],
            "stubtest of the stub against the module": [
                "-m",
                "mypy.stubtest",
                "archive_clone",
            ],
        }
        failed: list[str] = []
        for name, arguments in checks.items():
            print(f"check_typed_caller: {name}", flush=True)
            # From the scratch directory, so no configuration of this
            # repository's is read and the caller sees only what it installs.
            checked = subprocess.run([str(python), *arguments], check=False, cwd=root)
            if checked.returncode != 0:
                failed.append(name)
    if failed:
        print(f"check_typed_caller: failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
