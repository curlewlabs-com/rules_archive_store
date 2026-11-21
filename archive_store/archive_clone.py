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

By default directories become owner-writable so the caller can remove the tree.
Files keep their modes unless the caller sets writable_files to rewrite them.
Set writable_directories=False to skip adding write permission to directories. A
hard link within the tree becomes a file of its own, as clonefile(2) makes it.
A symlink whose resolution leaves the result fails it: the mount resolved that
link inside the expansion, as one that climbs out of a stripped prefix does,
and the result would read something else there. `destination` is a new path
only the caller writes; a failure leaves nothing at it.

This is standard-library Python, for a consumer whose action already runs
Python.
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
# cp goes on past each file it cannot clone, so its output can name every file
# in the tree; a failure reports this many of its lines and their count.
_SHOWN_ERRORS = 3


def clone(
    mount: Path,
    destination: Path,
    *,
    writable_files: bool = False,
    writable_directories: bool = True,
) -> int:
    """Clone the directory or regular file `mount` names to the new path `destination`.

    The source, its entries and their metadata must stay stable until the
    clone finishes, as they do in a held archive expansion.

    Returns how many entries were made owner-writable. Raises OSError where the
    platform or the volume cannot clone, FileExistsError when `destination`
    exists, and ValueError for a link whose resolution leaves the clone.
    """
    # Given the mount's link, clonefile(2) would clone the link itself.
    source = mount.resolve(strict=True)
    metadata = source.stat()
    mode = metadata.st_mode
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise ValueError(f"clone source is not a regular file or directory: {mount}")
    # Direct paths can carry privileges that the archive conversion strips.
    # The clone belongs to its new owner, so it must not acquire set-ID bits.
    if stat.S_ISREG(mode) and mode & (stat.S_ISUID | stat.S_ISGID):
        raise ValueError(f"clone source is a set-ID file: {mount}")
    # Each platform sits in its own arm: mypy skips an arm the checking
    # platform rules out, but reports code after an arm that always ends.
    if sys.platform == "darwin":
        if stat.S_ISREG(mode) or not writable_directories:
            _check_removable_source(source, metadata)
        make = functools.partial(_clonefile, source, destination)
    elif sys.platform == "linux":
        make = functools.partial(_reflink, source, destination)
    else:
        raise OSError(f"no copy-on-write clone on {sys.platform}: {destination}")
    return _create(
        destination, make, stat.S_IFMT(mode), writable_files, writable_directories
    )


def copy(mount: Path, destination: Path, *, writable_files: bool = False) -> int:
    """Copy a mounted directory for a host that cannot clone."""
    source = mount.resolve(strict=True)
    if not source.is_dir():
        raise NotADirectoryError(f"mount is not a directory: {mount}")
    make = functools.partial(shutil.copytree, source, destination, symlinks=True)
    return _create(destination, make, stat.S_IFDIR, writable_files, True)


def _create(
    destination: Path,
    make: Callable[[], object],
    kind: int,
    files: bool,
    directories: bool,
) -> int:
    # clonefile(2) would read such a path to its NUL and clone to the prefix,
    # where neither the existence check nor the cleanup looks.
    if "\0" in os.fspath(destination):
        raise ValueError(f"clone destination holds a NUL: {destination!r}")
    if os.path.lexists(destination):
        raise FileExistsError(f"clone destination exists: {destination}")
    try:
        make()
        if stat.S_IFMT(destination.lstat().st_mode) != kind:
            raise ValueError(
                f"clone has a different file type from its source: {destination}"
            )
        if kind == stat.S_IFREG:
            return _add_owner_write(destination) if files else 0
        return _finish(destination, files, directories)
    except BaseException:
        # A failed clone or copy can leave part of a tree, and nothing else
        # made one there, since the destination was absent.
        if os.path.lexists(destination):
            _discard(destination)
        raise


def _check_removable_source(source: Path, metadata: os.stat_result) -> None:
    # clonefile preserves BSD flags even on nested directories. Once it has
    # created a locked entry, failure cleanup may be unable to remove it.
    if sys.platform == "darwin":
        locked = (
            stat.UF_IMMUTABLE
            | stat.SF_IMMUTABLE
            | stat.UF_APPEND
            | stat.SF_APPEND
            | stat.SF_NOUNLINK
        )

        def check(path: Path, info: os.stat_result) -> None:
            if info.st_flags & locked:
                raise ValueError(f"clone source has a locked entry: {path}")

        check(source, metadata)
        pending = [source] if stat.S_ISDIR(metadata.st_mode) else []
        while pending:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    info = entry.stat(follow_symlinks=False)
                    check(path, info)
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(path)


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
    shown: list[str] = []
    errors = 0
    with subprocess.Popen(
        [cp, *arguments, "--", str(source), str(destination)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stderr is not None
        try:
            # Read as it comes, keeping only a sample, since the output can be
            # as long as the tree.
            for line in process.stderr:
                errors += 1
                if len(shown) < _SHOWN_ERRORS:
                    shown.append(line.rstrip("\n"))
        except BaseException:
            # Popen's exit waits only briefly on an interruption, and the
            # caller removes the tree next, so cp must be gone first.
            process.kill()
            process.wait()
            raise
    if process.returncode != 0:
        raise OSError(
            f"cp could not clone {source} to {destination}"
            f" (exit {process.returncode}, {errors} error lines): " + "; ".join(shown)
        )


def _finish(root: Path, files: bool, directories: bool) -> int:
    """Give owner write where the caller asked for it and check every link;
    return how many entries were made writable."""
    inside = Path(os.path.realpath(root))
    changed = _add_owner_write(root) if directories else 0
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as listing:
            for entry in listing:
                path = Path(entry.path)
                if entry.is_symlink():
                    if not _confined(path, inside):
                        raise ValueError(
                            f"link leaves the clone: {path} -> {os.readlink(path)}"
                        )
                elif entry.is_dir(follow_symlinks=False):
                    if directories:
                        changed += _add_owner_write(path)
                    pending.append(path)
                elif files and entry.is_file(follow_symlinks=False):
                    changed += _add_owner_write(path)
    return changed


def _confined(link: Path, root: Path) -> bool:
    """Whether `link`'s target, walked from the link's own directory a
    component at a time as the kernel resolves it, stays inside `root`.

    archive_mount.sh holds an expansion's links to the same rule. Checking only
    where a link ends would pass one that leaves the tree and comes back
    through what sits beside it, which the mount never read. A component that
    is not a directory ends the walk, as at a dangling link, which the store
    keeps; what remains of the target may not climb.
    """
    target = os.readlink(link)
    if target.startswith("/"):
        return False
    here = os.path.realpath(link.parent)
    parts = target.split("/")
    for index, part in enumerate(parts):
        if part in {"", "."}:
            continue
        step = os.path.join(here, part)
        if not os.path.isdir(step):
            return ".." not in parts[index + 1 :]
        here = os.path.realpath(step)
        if not Path(here).is_relative_to(root):
            return False
    return True


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
