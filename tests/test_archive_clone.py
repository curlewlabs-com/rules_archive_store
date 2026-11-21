"""A writable clone of a mount must be the expansion's tree, and only that tree.

The expansion is shared by every action on the instance, so no write to the
clone may reach it, and the clone must be removable by whatever cleans up the
action, though the store made every directory read-only. Where the volume
cannot clone, the helper must fail and leave nothing, rather than copy a tree
whose size is the cost the store exists to remove.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import stat
import sys
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from typing import override

archive_clone = import_module("archive_clone")

_TOOL = b"#!/bin/sh\nexit 0\n"
_DATA = b"payload\n" * 512
# A time no clone would land on by itself, so a kept time is told from a new one.
_MTIME_NS = 1_000_000_000_000_000_000


# linux/fs.h's FICLONE, _IOW(0x94, 9, int).
_FICLONE = 0x40049409


def _volume_clones(path: Path) -> bool:
    """Whether the volume at `path` serves copy-on-write clones, asked of the
    volume directly rather than through the helper under test."""
    # The macOS suites need APFS (CONTRIBUTING.md), which always clones.
    if sys.platform == "darwin":
        return True
    if sys.platform != "linux":
        return False
    probe = Path(tempfile.mkdtemp(dir=path))
    (probe / "source").write_bytes(b"probe")
    with (probe / "source").open("rb") as source, (probe / "clone").open("wb") as clone:
        try:
            fcntl.ioctl(clone.fileno(), _FICLONE, source.fileno())
        except OSError:
            return False
    return True


class ArchiveCloneTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(dir=os.environ.get("TEST_TMPDIR"))).resolve()
        self.addCleanup(self._remove_base)
        self.expansion = self.base / "store" / "expansion"
        self._make_expansion()
        # The store's mount is a link to its expansion.
        self.mount = self.base / "mount"
        self.mount.symlink_to(self.expansion)
        self.destination = self.base / "action" / "sdk"
        self.destination.parent.mkdir()

    def _make_expansion(self) -> None:
        (self.expansion / "bin").mkdir(parents=True)
        (self.expansion / "lib").mkdir()
        (self.expansion / "empty").mkdir()
        (self.expansion / "bin" / "tool").write_bytes(_TOOL)
        (self.expansion / "bin" / "tool").chmod(0o555)
        (self.expansion / "lib" / "data").write_bytes(_DATA)
        (self.expansion / "lib" / "data").chmod(0o444)
        os.utime(self.expansion / "lib" / "data", ns=(_MTIME_NS, _MTIME_NS))
        os.link(self.expansion / "lib" / "data", self.expansion / "lib" / "twin")
        (self.expansion / "alias").symlink_to("lib/data")
        for directory in ["bin", "lib", "empty", "."]:
            (self.expansion / directory).chmod(0o555)

    def _remove_base(self) -> None:
        for directory, _, _ in os.walk(self.base):
            Path(directory).chmod(0o755)
        shutil.rmtree(self.base)

    def _assert_writable_tree(self, changed: int, *, writable_files: bool) -> None:
        root = self.destination
        self.assertFalse(root.is_symlink())
        self.assertEqual((root / "bin" / "tool").read_bytes(), _TOOL)
        self.assertEqual((root / "lib" / "data").read_bytes(), _DATA)
        self.assertTrue((root / "empty").is_dir())
        self.assertEqual(os.readlink(root / "alias"), "lib/data")
        self.assertEqual(
            (root / "lib" / "data").stat().st_mtime_ns,
            _MTIME_NS,
            "the clone keeps the expansion's times",
        )
        for directory in [root, root / "bin", root / "lib", root / "empty"]:
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
        files = {"bin/tool": 0o555, "lib/data": 0o444, "lib/twin": 0o444}
        for name, mode in files.items():
            expected = mode | stat.S_IWUSR if writable_files else mode
            self.assertEqual(stat.S_IMODE((root / name).stat().st_mode), expected)
        self.assertEqual(changed, 4 + (len(files) if writable_files else 0))
        # A write to the clone must not reach the expansion it shares blocks with.
        (root / "lib" / "data").chmod(0o644)
        (root / "lib" / "data").write_bytes(b"rewritten\n")
        self.assertEqual((self.expansion / "lib" / "data").read_bytes(), _DATA)
        # A hard link becomes a file of its own, as clonefile(2) makes it, on
        # every platform and in the copy alike.
        self.assertEqual((root / "lib" / "twin").read_bytes(), _DATA)
        # Whatever cleans up the action removes the clone without fixing modes.
        shutil.rmtree(root)

    def test_clone_is_the_expansion_made_removable(self) -> None:
        if not _volume_clones(self.base):
            self.skipTest("this volume serves no copy-on-write clones")
        changed = archive_clone.clone(self.mount, self.destination)
        self._assert_writable_tree(changed, writable_files=False)

    def test_clone_can_make_files_writable(self) -> None:
        if not _volume_clones(self.base):
            self.skipTest("this volume serves no copy-on-write clones")
        changed = archive_clone.clone(self.mount, self.destination, writable_files=True)
        self._assert_writable_tree(changed, writable_files=True)

    def test_clone_fails_rather_than_copying(self) -> None:
        if _volume_clones(self.base):
            self.skipTest("this volume serves copy-on-write clones")
        with self.assertRaises(OSError):
            archive_clone.clone(self.mount, self.destination)
        self.assertFalse(os.path.lexists(self.destination))

    def test_copy_is_the_same_tree_anywhere(self) -> None:
        changed = archive_clone.copy(self.mount, self.destination)
        self._assert_writable_tree(changed, writable_files=False)

    def test_an_existing_destination_is_left_alone(self) -> None:
        # The helper removes a partial tree only when it made one; an existing
        # destination is something else's and must survive the refusal.
        self.destination.mkdir()
        (self.destination / "kept").write_bytes(b"")
        for make in [archive_clone.clone, archive_clone.copy]:
            with self.assertRaises(FileExistsError):
                make(self.mount, self.destination)
            self.assertTrue((self.destination / "kept").exists())

    def test_a_link_that_leaves_the_mount_is_refused(self) -> None:
        # Through the mount each link resolves inside the expansion's archive,
        # beside the stripped prefix. In the clone one would name whatever sits
        # beside the destination, and the other leaves the clone only to come
        # back into it, where it reads what the mount never did.
        (self.expansion.parent / "other").mkdir()
        (self.expansion.parent / "other" / "data").write_bytes(b"archive\n")
        (self.destination.parent / "other").mkdir()
        (self.destination.parent / "other" / "data").write_bytes(b"action\n")
        makes = [archive_clone.copy]
        if _volume_clones(self.base):
            makes.append(archive_clone.clone)
        for target in ["../other/data", f"../{self.destination.name}/lib/data"]:
            self.expansion.chmod(0o755)
            (self.expansion / "up").unlink(missing_ok=True)
            (self.expansion / "up").symlink_to(target)
            self.expansion.chmod(0o555)
            for make in makes:
                with self.subTest(target=target, make=make.__name__):
                    with self.assertRaises(ValueError):
                        make(self.mount, self.destination)
                    self.assertFalse(os.path.lexists(self.destination))

    def test_a_destination_holding_a_nul_is_refused(self) -> None:
        # Native code would read the path only to its NUL and make the tree at
        # the prefix, which no cleanup would find.
        with self.assertRaises(ValueError):
            archive_clone.clone(self.mount, Path(f"{self.destination}\0rest"))
        self.assertFalse(os.path.lexists(self.destination))

    def test_a_file_mount_clones_an_independent_file(self) -> None:
        if not _volume_clones(self.base):
            self.skipTest("this volume serves no copy-on-write clones")
        original = self.expansion / "lib" / "data"
        file_mount = self.base / "file-mount"
        file_mount.symlink_to(original)
        for writable in (False, True):
            with self.subTest(writable=writable):
                changed = archive_clone.clone(
                    file_mount, self.destination, writable_files=writable
                )
                self.assertFalse(self.destination.is_symlink())
                self.assertEqual(self.destination.read_bytes(), _DATA)
                self.assertEqual(self.destination.stat().st_mtime_ns, _MTIME_NS)
                self.assertEqual(
                    stat.S_IMODE(self.destination.stat().st_mode),
                    0o644 if writable else 0o444,
                )
                self.assertEqual(changed, int(writable))
                self.destination.chmod(0o644)
                self.destination.write_bytes(b"changed")
                self.assertEqual(original.read_bytes(), _DATA)
                self.destination.unlink()

    def test_sealed_directories_keep_modes_and_link_checks(self) -> None:
        if not _volume_clones(self.base):
            self.skipTest("this volume serves no copy-on-write clones")
        for files in (False, True):
            with self.subTest(writable_files=files):
                root = self.base / f"sealed-{files}"
                changed = archive_clone.clone(
                    self.mount, root, writable_directories=False, writable_files=files
                )
                self.assertEqual(changed, 3 if files else 0)
                for path in (root, root / "lib"):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o555)
                self.assertEqual(
                    stat.S_IMODE((root / "lib/data").stat().st_mode),
                    0o644 if files else 0o444,
                )
                self.assertEqual((root / "alias").read_bytes(), _DATA)
        # A read-only partial tree must still disappear on a rejected link.
        self.expansion.chmod(0o755)
        (self.expansion / "up").symlink_to("../elsewhere")
        with self.assertRaises(ValueError):
            archive_clone.clone(
                self.mount, self.base / "rejected", writable_directories=False
            )
        self.assertFalse(os.path.lexists(self.base / "rejected"))

    def test_locked_file_is_refused_before_it_can_lock_a_clone(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("macOS file flags")
        source = self.base / "locked"
        source.write_bytes(_DATA)
        os.chflags(source, stat.UF_IMMUTABLE)
        self.addCleanup(os.chflags, source, 0)
        with self.assertRaises(ValueError):
            archive_clone.clone(source, self.destination, writable_files=True)
        self.assertFalse(os.path.lexists(self.destination))

    def test_special_source_is_refused_before_cloning(self) -> None:
        fifo = self.base / "fifo"
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            archive_clone.clone(fifo, self.destination)
        self.assertFalse(os.path.lexists(self.destination))


if __name__ == "__main__":
    unittest.main()
