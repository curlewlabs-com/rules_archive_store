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
that only the caller may create or change until the call returns. Its parent
must permit the caller to remove entries on failure, including under any ACLs.

This is standard-library Python, for a consumer whose action already runs
Python.
"""

from __future__ import annotations

import ctypes
import errno
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
    materialize_file_links: bool = False,
) -> int:
    """Clone the directory or regular file `mount` names to the new path `destination`.

    The source, its entries and their metadata must stay stable until the
    clone finishes, as they do in a held archive expansion. The destination
    path is exclusively the caller's until return, and its parent must let the
    caller create and remove its private clone. With materialize_file_links,
    the caller also supplies and holds every link's final regular file, even
    where that file lies outside the tree.

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
    # On Darwin these modes must avoid the ACL changes clonefile makes.
    # The original directory operation keeps its native ACL inheritance.
    mode_only = stat.S_ISREG(mode) or not writable_directories or materialize_file_links
    links = _check_source(source, metadata, mode_only, materialize_file_links)
    # Each platform sits in its own arm: mypy skips an arm the checking
    # platform rules out, but reports code after an arm that always ends.
    if sys.platform == "darwin":
        if mode_only:
            _check_acl(destination.parent.resolve(strict=True))
        make = functools.partial(_clone_macos, source, destination, links)
    elif sys.platform == "linux":
        make = functools.partial(_reflink, source, destination, materialize_file_links)
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
        # may create it during this call under the exclusive-path contract.
        if os.path.lexists(destination):
            _discard(destination)
        raise


def _check_source(
    source: Path,
    metadata: os.stat_result,
    mode_only: bool,
    materialize_file_links: bool,
) -> dict[Path, list[tuple[str, Path]]]:
    def check(path: Path, info: os.stat_result) -> None:
        if not (
            stat.S_ISREG(info.st_mode)
            or stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
        ):
            raise ValueError(f"clone source has a special entry: {path}")
        # Unlike archive conversion, a direct source can carry a set-ID mode.
        # The new owner must not acquire it on either a file or a tree clone.
        if stat.S_ISREG(info.st_mode) and info.st_mode & (stat.S_ISUID | stat.S_ISGID):
            raise ValueError(f"clone source is a set-ID file: {path}")
        if sys.platform == "darwin":
            # Once clonefile creates a locked descendant, failure cleanup may
            # be unable to remove it, regardless of write permission.
            locked = (
                stat.UF_IMMUTABLE
                | stat.SF_IMMUTABLE
                | stat.UF_APPEND
                | stat.SF_APPEND
                | stat.SF_NOUNLINK
            )
            if mode_only:
                _check_acl(path)
            if info.st_flags & locked:
                raise ValueError(f"clone source has a locked entry: {path}")

    links: dict[Path, list[tuple[str, Path]]] = {}
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
                elif materialize_file_links and stat.S_ISLNK(info.st_mode):
                    target = path.resolve(strict=True)
                    target_info = target.stat()
                    if not stat.S_ISREG(target_info.st_mode):
                        raise ValueError(
                            f"materialized link must name a regular file: {path}"
                        )
                    check(target, target_info)
                    if sys.platform == "darwin":
                        links.setdefault(path.parent.relative_to(source), []).append(
                            (entry.name, target)
                        )
    return links


def _check_acl(source: Path) -> None:
    # clonefile omits source ACLs and inherits destination entries. Either can
    # change access even when all POSIX mode bits are preserved.
    get = _library().acl_get_link_np
    get.argtypes = [ctypes.c_char_p, ctypes.c_int]
    get.restype = ctypes.c_void_p
    # sys/acl.h: ACL_TYPE_EXTENDED. Darwin returns ENOENT for an absent ACL.
    acl = get(os.fsencode(source), 0x100)
    if acl is None:
        code = ctypes.get_errno()
        if code == errno.ENOENT:
            return
        raise OSError(code, os.strerror(code), str(source))
    free = _library().acl_free
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_int
    free(acl)
    raise ValueError(f"clone requires a path without an extended ACL: {source}")


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


def _clone_macos(
    source: Path, destination: Path, links: dict[Path, list[tuple[str, Path]]]
) -> None:
    _clonefile(source, destination)
    # clonefile handles an ordinary tree in one call. Only parents containing
    # Bazel-style file links need extra directory writes and a file clone.
    for relative, children in links.items():
        parent = destination / relative
        info = (source / relative).stat()
        mode = stat.S_IMODE(parent.stat().st_mode)
        if not mode & stat.S_IWUSR:
            parent.chmod(mode | stat.S_IWUSR)
        for name, target in children:
            child = parent / name
            child.unlink()
            _clonefile(target, child)
        if not mode & stat.S_IWUSR:
            parent.chmod(mode)
        # Replacing the directory entries must not introduce the wall clock
        # into builds that preserve their input directory timestamps.
        os.utime(parent, ns=(info.st_atime_ns, info.st_mtime_ns))


def _reflink(source: Path, destination: Path, materialize_file_links: bool) -> None:
    cp = shutil.which("cp", path=_SYSTEM_PATH)
    if cp is None:
        raise OSError(f"no cp on {_SYSTEM_PATH} to clone {source}")
    # --reflink=always fails a file the volume cannot clone, where auto would
    # copy it. Mode and times are kept as clonefile(2) keeps them.
    arguments = [
        "-R",
        "-L" if materialize_file_links else "-P",
        "--reflink=always",
        "--preserve=mode,timestamps",
    ]
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
