"""Clone a mounted archive's expansion into a tree a command can write.

A mount (archive_mount.sh) is a link to the store's expansion of an archive,
which is read-only and shared by every action on the executor instance. A
command that writes into a tree it reads, as many SDKs and build tools do,
needs a tree of its own. `clone` gives it one whose files share the expansion's
blocks until either is written, so it costs metadata rather than the tree's
bytes and no write reaches the expansion: one clonefile(2) of the whole
directory on macOS, and on Linux GNU cp's reflink of each file, which XFS and
Btrfs serve. A clone needs its destination on the store's volume, where an
action's own directory is under the executor contract (docs/executor.md).

Where the volume cannot clone, `clone` fails rather than copying the bytes,
since a silent copy is the cost the store exists to remove. `copy` is that
copy, for a caller that chose it on a host with no copy-on-write filesystem.

Each leaves every directory owner-writable, so whatever removes the tree can,
and with `writable_files` every regular file too, for a command that rewrites
them; otherwise files keep the store's modes, which saves a call per file.
Either way nothing is left at the destination when it fails. This is
standard-library Python, for a consumer whose action already runs Python.
"""

from __future__ import annotations

import ctypes
import functools
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

# The utilities come from the system, whatever PATH the action has, as
# archive_mount.sh's do.
_SYSTEM_PATH = "/usr/bin:/bin"


def clone(mount: Path, destination: Path, *, writable_files: bool = False) -> int:
    """Clone the tree `mount` names to the new path `destination`.

    Returns how many entries were made owner-writable. Raises OSError where the
    platform or the volume cannot clone, and FileExistsError when
    `destination` exists.
    """
    source = _expansion(mount)
    # Each platform sits in its own arm: mypy skips an arm the checking
    # platform rules out, but reports code after an arm that always ends.
    if sys.platform == "darwin":
        _create(destination, lambda: _clonefile(source, destination))
    elif sys.platform == "linux":
        _create(destination, lambda: _reflink(source, destination))
    else:
        raise OSError(f"no copy-on-write clone on {sys.platform}: {destination}")
    return _make_writable(destination, writable_files)


def copy(mount: Path, destination: Path, *, writable_files: bool = False) -> int:
    """`clone` as a plain copy of every byte, for a host that cannot clone."""
    source = _expansion(mount)
    _create(destination, lambda: shutil.copytree(source, destination, symlinks=True))
    return _make_writable(destination, writable_files)


def _expansion(mount: Path) -> Path:
    # Given the mount's link, clonefile(2) would clone the link itself.
    source = mount.resolve(strict=True)
    if not source.is_dir():
        raise NotADirectoryError(f"mount is not a directory: {mount}")
    return source


def _create(destination: Path, make: Callable[[], object]) -> None:
    if os.path.lexists(destination):
        raise FileExistsError(f"clone destination exists: {destination}")
    try:
        make()
    except BaseException:
        # A failed clone or copy can leave part of a tree, and nothing else
        # made one there, since the destination was absent.
        if os.path.lexists(destination):
            _discard(destination)
        raise


@functools.cache
def _library() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def _clonefile(source: Path, destination: Path) -> None:
    call = _library().clonefile
    call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    call.restype = ctypes.c_int
    if call(os.fsencode(source), os.fsencode(destination), 0) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(source), None, str(destination))


def _reflink(source: Path, destination: Path) -> None:
    cp = shutil.which("cp", path=_SYSTEM_PATH)
    if cp is None:
        raise OSError(f"no cp on {_SYSTEM_PATH} to clone {source}")
    # --reflink=always fails a file the volume cannot clone, where auto would
    # copy it. Mode and times are kept as clonefile(2) keeps them.
    arguments = ["-R", "-P", "--reflink=always", "--preserve=mode,timestamps"]
    result = subprocess.run(
        [cp, *arguments, "--", str(source), str(destination)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise OSError(
            f"cp could not clone {source} to {destination}"
            f" (exit {result.returncode}): {result.stderr.strip()}"
        )


def _make_writable(root: Path, files: bool) -> int:
    def unreadable(error: OSError) -> None:
        # os.walk skips a directory it cannot list unless told otherwise,
        # which would leave that subtree unremovable.
        raise error

    changed = 0
    for directory, _, names in os.walk(root, onerror=unreadable):
        changed += _add_owner_write(Path(directory))
        if files:
            for name in names:
                path = Path(directory, name)
                if stat.S_ISREG(path.lstat().st_mode):
                    changed += _add_owner_write(path)
    return changed


def _add_owner_write(path: Path) -> int:
    mode = path.lstat().st_mode
    if mode & stat.S_IWUSR:
        return 0
    path.chmod(stat.S_IMODE(mode) | stat.S_IWUSR)
    return 1


def _discard(root: Path) -> None:
    if not stat.S_ISDIR(root.lstat().st_mode):
        root.unlink()
        return
    # A cloned directory keeps its source's mode, and removing its entries
    # needs owner permission on it.
    pending = [root]
    while pending:
        directory = pending.pop()
        directory.chmod(stat.S_IRWXU)
        with os.scandir(directory) as listing:
            pending.extend(
                Path(item.path)
                for item in listing
                if item.is_dir(follow_symlinks=False)
            )
    shutil.rmtree(root)
