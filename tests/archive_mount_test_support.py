"""The harness the archive_mount.sh suites share: the real helper, run with
the system shell and archive tools it uses on a worker, as an action whose
input root holds a tar as the conversion writes it, against a store in a
scratch directory.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tarfile
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from typing import override

import archive_parts

INFINITE: float = import_module("unittest_timeouts").INFINITE

HELPER = Path(__file__).parent.parent / "archive_store" / "archive_mount.sh"
# The store namespace the helper above expands into: its own digest's prefix.
NAMESPACE = hashlib.sha256(HELPER.read_bytes()).hexdigest()[:16]

_FILES = {
    "tool-1.0/bin/run": (b"#!/bin/sh\necho run\n", 0o755),
    "tool-1.0/share/data.txt": (b"payload\n", 0o644),
}
_LINK = ("tool-1.0/bin/alias", "run")
_EMPTY_DIR = "tool-1.0/empty"


def tar_bytes(link: str | None = None, up: bool = False) -> bytes:
    """The fixture tree as a tar, plus a link `share/extra` to `link` and, with
    `up`, a link `up` to the tree's parent directory."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in ("tool-1.0", "tool-1.0/bin", "tool-1.0/share", _EMPTY_DIR):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            archive.addfile(info)
        for name, (content, mode) in _FILES.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = mode
            archive.addfile(info, io.BytesIO(content))
        info = tarfile.TarInfo(_LINK[0])
        info.type = tarfile.SYMTYPE
        info.linkname = _LINK[1]
        archive.addfile(info)
        extras = {"tool-1.0/share/extra": link} if link else {}
        if up:
            extras["tool-1.0/up"] = ".."
        for name, target in extras.items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)
    return buffer.getvalue()


class ArchiveMountCase(unittest.TestCase):
    @override
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self._remove, scratch)
        self.root = Path(scratch.name)
        self.store = self.root / "store"
        self.store.mkdir()
        self.configured = True
        # What RULES_ARCHIVE_STORE_DIR says, when not the store's absolute path.
        self.store_setting: str | None = None
        # More of the action's environment, as a hostile caller might set it.
        self.action_env: dict[str, str] = {}

    def _remove(self, scratch: tempfile.TemporaryDirectory[str]) -> None:
        # Expansions are read-only; the scratch directory cannot be removed
        # until they are writable again.
        for directory, names, _ in os.walk(scratch.name):
            for name in names:
                path = Path(directory, name)
                if not path.is_symlink():
                    path.chmod(0o755)
        scratch.cleanup()

    def _archive(
        self, name: str, content: bytes, part_bytes: int | None = None
    ) -> tuple[Path, str]:
        """The tar `content` as the conversion writes it, a directory `name` of
        its parts, each at most `part_bytes`: one part unless given."""
        path = self.root / name
        path.mkdir()
        step = part_bytes or len(content)
        for index, start in enumerate(range(0, len(content), step)):
            part = archive_parts.part_name(archive_parts.TAR_NAME, index)
            (path / part).write_bytes(content[start : start + step])
        return path, hashlib.sha256(content).hexdigest()

    def _action(
        self,
        name: str,
        manifest: str,
        archive: Path,
        command: list[str],
        extra: dict[str, str] | None = None,
        helper: Path = HELPER,
        umask: int = 0o022,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        """Start the helper as an action whose input root holds the archive,
        with `env` added to its environment."""
        workdir = self.root / name
        workdir.mkdir(exist_ok=True)
        (workdir / archive.name).symlink_to(archive)
        (workdir / "manifest").write_text(manifest, encoding="ascii")
        for file_name, content in (extra or {}).items():
            (workdir / file_name).write_text(content, encoding="ascii")
        env = {
            "PATH": "/usr/bin:/bin",
            "TMPDIR": str(self.root),
            **self.action_env,
            **(env or {}),
        }
        if self.configured:
            env["RULES_ARCHIVE_STORE_DIR"] = self.store_setting or str(self.store)
        return subprocess.Popen(
            ["/bin/sh", str(helper), "manifest", "--", *command],
            cwd=workdir,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # Its own process group, which a test signals whole, as a runner
            # killing an action does.
            start_new_session=True,
            stderr=subprocess.PIPE,
            umask=umask,
        )

    def _run(
        self,
        name: str,
        manifest: str,
        archive: Path,
        command: list[str],
        extra: dict[str, str] | None = None,
        helper: Path = HELPER,
        umask: int = 0o022,
    ) -> subprocess.CompletedProcess[bytes]:
        process = self._action(
            name, manifest, archive, command, extra, helper, umask=umask
        )
        stdout, stderr = process.communicate(timeout=INFINITE)
        return subprocess.CompletedProcess(
            process.args, process.returncode, stdout, stderr
        )

    def _published(self) -> list[Path]:
        namespace = self.store / NAMESPACE
        if not namespace.is_dir():
            return []
        return sorted(path for path in namespace.iterdir() if path.is_symlink())

    def _objects(self) -> list[Path]:
        objects = self.store / NAMESPACE / "objects"
        return sorted(objects.iterdir()) if objects.is_dir() else []
