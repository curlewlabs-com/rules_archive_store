"""Evict the least recently used expansions from an archive store, within a budget.

The store, its layout and its holds are archive_mount.sh's
(docs/store.md). In each namespace a published entry is a symlink
named for an archive's digest, naming `objects/<object>`; `used/<digest>` is
touched on every read; `holds/<object>@<pid>-<start>` names a process reading
the object, and an object's own name carries the identity of the process that
staged it. Neither the helper nor anything else removes a hold, so a hold is
live exactly while `ps` still reports that start time for that PID.

One pass:

1. Finishes deleting what an earlier pass moved into `<store>/.trash`, which is
   where an interrupted delete resumes.
2. Drops every hold whose process has gone.
3. Removes each object no entry publishes, once nothing holds it and the
   process that staged it has gone: a killed expansion, or an eviction an
   earlier pass had to leave because something held it. Only its creator ever
   publishes an object, so once the creator has gone, a fresh read of the
   entries settles whether it is published.
4. Retires each namespace with nothing in it: no object, no entry and no hold,
   not even a helper's hold on the namespace itself. Only a namespace marked
   `.claims` is retired, since only its helpers take that hold. It marks the namespace
   `.retiring`, reads it again, and renames it into the trash only if it is
   still empty. The helper holds its namespace before it reads the marker, so
   either this read sees that hold and keeps the namespace, or the helper sees
   the marker or its hold gone with the renamed namespace, and claims the
   namespace afresh.
5. While the store's objects exceed the budget, takes the least recently used
   entry, unpublishes it, and only then reads the holds on its object. The
   helper takes its hold before it re-reads the entry, so every read that
   relied on the entry holds the object by now. An object nothing holds is
   renamed into the trash and deleted; a held one stays unpublished for a later
   pass. An entry read since the pass began is passed over. While the trash
   holds a tree it could not delete, nothing new goes into it.

The budget is a ceiling. Over it, the least recently used entry goes whatever
its age; only a live hold keeps a tree, so a store whose every tree is held
stays over budget until those actions finish, and a pass that ends over budget
says why. An eviction of a tree read within the keep window is logged as
thrash, a sign the budget is smaller than what the worker's actions read. An
evicted entry's use record goes with it once no entry names its digest, and a
pass drops any use record whose digest no entry names, which an eviction whose
delete was cut short leaves behind.

The budget counts trees, not the namespaces' own directories, entries, use
records and holds. Those are bounded without it: an entry and its use record
come and go with the object the budget counts, a hold outlives its process only
until the next pass, and a namespace goes once it is empty. Counting them would
not govern them either, since a directory's blocks need not shrink when
entries leave it, so a store over budget on their account would evict trees
for bytes their removal never returns.

A namespace with no `holds` directory would come from a helper revision from
before holds, whose readers leave nothing to check. Such a helper reads a store
variable nothing sets (archive_mount.sh says which), so no such namespace
reaches a worker's store; one found anyway is counted and left alone.

Liveness is one `ps` listing of every process, read after the names it judges
and before anything is removed on its word, so a PID reissued between one
judgment and the next cannot carry an earlier reading into a later one. A pass
takes one once every namespace's holds and objects are read, none when no
namespace holds either, and one more for each eviction's check of its holds. A
tree is renamed out of `objects` before any of it is deleted, so no reader ever
sees one half-deleted. Every decision fails toward keeping: a listing `ps` cannot
make stops the pass, and an unpublished object whose creator its name does not
identify is never removed.

The evictor runs as the worker's own account, which can remove any slot
account's read-only trees, and serializes on `<store>/.evict.lock`, so a hand
run cannot race the daemon. A pass opens the store's directory once and reaches
everything inside it through that descriptor, never through the store's path
again: a worker start that sets the store aside and makes a new one under the
same path leaves the pass working on the store it locked, the one set aside,
and nothing it does reaches the new one.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

_NAMESPACE = re.compile(r"[0-9a-f]{16}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_IDENTITY = r"(?P<pid>[0-9]+)-(?P<started>[0-9A-Za-z]+)"
_OBJECT = re.compile(rf"[0-9a-f]{{64}}\.{_IDENTITY}\.[0-9A-Za-z]+")
_HOLD = re.compile(rf"(?P<object>[^@]+)@{_IDENTITY}")
_TRASH = ".trash"
# Created in a namespace the pass is about to retire; archive_mount.sh reads it.
_RETIRING = ".retiring"
# Marks a namespace whose helpers claim it; archive_mount.sh says why only
# such a namespace is retired.
_CLAIMS = ".claims"
# A namespace's own directories, which a helper makes before it stages.
_PARTS = ("objects", "holds", "used")
_LOCK = ".evict.lock"
# How the helper reads a start time; reading it the same way here is what makes
# a hold's start time comparable with the process's.
_PS_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "TZ": "UTC0"}
_PS_TIMEOUT_SECONDS = 30
# `ps -o lstart=` prints weekday, month, day, time and year as separate tokens
# on Linux and on macOS.
_LSTART_TOKENS = 5
# Passes in a row that fail before the evictor exits with an error, so the
# service that runs it records the failure where the guest's health probe
# reads it, rather than logging it forever while the store grows.
_FAILED_PASSES_BEFORE_EXIT = 3
# A directory is opened without following a link in its own name, so a link
# planted where a namespace or its parts belong leads nowhere.
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def log(message: str) -> None:
    print(f"archive_store_evict: {message}", file=sys.stderr, flush=True)


class NoListing(Exception):
    """ps could not list the processes a removal must be judged against."""


@dataclass(frozen=True)
class Processes:
    """Every running process's start time, as the helper writes it, by PID."""

    started: dict[int, str]

    def gone(self, pid: int, started: str) -> bool:
        """The process a name identifies is no longer running, so a different
        start time at its PID is a different process."""
        return self.started.get(pid) != started


def list_processes() -> Processes | None:
    """One listing of every process; None when ps could not make it."""
    try:
        result = subprocess.run(
            ["ps", "-A", "-o", "pid=", "-o", "lstart="],
            env=_PS_ENV,
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        log(f"cannot list processes with ps ({error}); stopping this pass")
        return None
    if result.returncode != 0:
        log(f"ps failed ({result.stderr.strip()}); stopping this pass")
        return None
    started: dict[int, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        if len(fields) != 1 + _LSTART_TOKENS or not fields[0].isdigit():
            log(f"cannot read ps line {line!r}; stopping this pass")
            return None
        started[int(fields[0])] = "".join(
            char for char in "".join(fields[1:]) if char.isalnum()
        )
    if not started:
        log("ps listed no processes; stopping this pass")
        return None
    return Processes(started)


class Directory:
    """A directory reached through a descriptor, so a rename of it or of any
    directory above it does not change what its names mean."""

    def __init__(self, fd: int, label: str) -> None:
        self.fd = fd
        self.label = label

    def child(self, name: str) -> Directory | None:
        """The directory `name` inside this one; None when there is none."""
        try:
            fd = os.open(name, _DIRECTORY, dir_fd=self.fd)
        except FileNotFoundError, NotADirectoryError:
            return None
        except OSError as error:
            # O_NOFOLLOW meeting a link: not a directory of the layout.
            if error.errno == errno.ELOOP:
                return None
            raise
        return Directory(fd, f"{self.label}/{name}")

    def names(self) -> list[str]:
        return sorted(os.listdir(self.fd))

    def is_symlink(self, name: str) -> bool:
        try:
            return stat.S_ISLNK(
                os.stat(name, dir_fd=self.fd, follow_symlinks=False).st_mode
            )
        except FileNotFoundError:
            return False

    def is_dir(self, name: str) -> bool:
        try:
            return stat.S_ISDIR(
                os.stat(name, dir_fd=self.fd, follow_symlinks=False).st_mode
            )
        except FileNotFoundError:
            return False

    def readlink(self, name: str) -> str | None:
        """The link's target; None when it is gone or is not a link. Any other
        failure is raised, since reading a published entry as absent would
        leave its object to be removed as unpublished."""
        try:
            return os.readlink(name, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno == errno.EINVAL:
                return None
            raise

    def mtime(self, name: str) -> float | None:
        try:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False).st_mtime
        except FileNotFoundError:
            return None

    def unlink(self, name: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=self.fd)

    def close(self) -> None:
        os.close(self.fd)


def delete_tree(name: str, parent: int) -> None:
    """Remove the tree `name` inside the directory `parent`, whose own
    directories may carry no write bit."""
    for _, _, _, fd in os.fwalk(name, dir_fd=parent):
        os.fchmod(fd, stat.S_IRWXU)
    shutil.rmtree(name, dir_fd=parent)


def tree_bytes(name: str, parent: int) -> int:
    """The disk the tree `name` inside `parent` takes, from its entries'
    allocated blocks."""
    total = os.stat(name, dir_fd=parent, follow_symlinks=False).st_blocks * 512
    for _, names, files, fd in os.fwalk(name, dir_fd=parent):
        for entry in (*names, *files):
            total += os.stat(entry, dir_fd=fd, follow_symlinks=False).st_blocks * 512
    return total


@dataclass(frozen=True)
class Namespace:
    directory: Directory
    objects: Directory | None
    holds: Directory | None
    used: Directory | None

    @property
    def name(self) -> str:
        return self.directory.label.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Scanned:
    """A namespace's holds and objects, read before the listing that judges
    them."""

    name: str
    holds: list[tuple[str, str, int, str]]
    objects: list[str]


@dataclass(frozen=True)
class Entry:
    # Its namespace's name, reopened from the store when the entry is
    # evicted, so a pass holds one namespace's descriptors at a time.
    namespace: str
    digest: str
    object: str
    used: float


class Evictor:
    def __init__(
        self,
        store: Path,
        budget_bytes: int,
        keep_seconds: float,
        delete: Callable[[str, int], None] = delete_tree,
        processes: Callable[[], Processes | None] = list_processes,
    ) -> None:
        self.store = store
        self.budget_bytes = budget_bytes
        self.keep_seconds = keep_seconds
        self._delete = delete
        self._processes = processes
        # Published objects never change, so their sizes are read once; a tree
        # still being staged is read afresh every pass.
        self._sizes: dict[tuple[str, str], int] = {}
        self._draining = True
        self._reclaimed = True

    def run_pass(self, now: float) -> bool:
        """One pass; whether it completed. One skipped behind another
        evictor's lock, or with no store to evict from, counts as completed;
        one that could not move or delete a tree it meant to remove did not,
        since that is the failure that leaves the store growing."""
        self._reclaimed = True
        try:
            root = os.open(self.store, _DIRECTORY)
        except FileNotFoundError, NotADirectoryError:
            log(f"no store at {self.store}; nothing to evict")
            return True
        with contextlib.ExitStack() as opened:
            opened.callback(os.close, root)
            lock = os.open(_LOCK, os.O_RDWR | os.O_CREAT, 0o600, dir_fd=root)
            opened.callback(os.close, lock)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log(f"another evictor holds {self.store / _LOCK}; skipping this pass")
                return True
            try:
                self._pass(Directory(root, str(self.store)), opened, now)
            except NoListing:
                # list_processes said why.
                return False
            return self._reclaimed

    def _reading(self) -> Processes:
        """A listing of the processes, taken now; a pass without one stops."""
        processes = self._processes()
        if processes is None:
            raise NoListing
        return processes

    def _pass(self, store: Directory, opened: contextlib.ExitStack, now: float) -> None:
        # Bytes the trash still holds count against the budget, and while it
        # holds a tree it could not delete, nothing new goes into it.
        self._store = store
        self._opened = opened
        self._trash: Directory | None = None
        total, self._draining = self._empty_trash(opened)
        entries: list[Entry] = []
        sizes, self._sizes = self._sizes, {}
        scanned: list[Scanned] = []
        for name in self._namespace_names():
            with self._namespace(name) as namespace:
                if namespace is None:
                    continue
                if namespace.holds is None:
                    total += self._foreign(namespace)
                    continue
                holds = list(self._holds(namespace))
                scanned.append(Scanned(name, holds, self._objects(namespace)))
        # Read after every name it judges, so a PID it calls gone is not one a
        # process listed above has since been given.
        processes = (
            self._reading()
            if any(found.holds or found.objects for found in scanned)
            else None
        )
        for found in scanned:
            with self._namespace(found.name) as namespace:
                if namespace is None:
                    continue
                if processes is not None:
                    total += self._sweep(namespace, found, processes, sizes, entries)
                self._drop_unnamed_use_records(namespace)
                self._retire(namespace)
        for entry in sorted(entries, key=lambda item: item.used):
            if total <= self.budget_bytes or not self._draining:
                break
            recent = now - entry.used <= self.keep_seconds
            with self._namespace(entry.namespace) as namespace:
                freed = 0 if namespace is None else self._evict(namespace, entry, now)
            if freed and recent:
                log(
                    f"thrash: evicted {entry.namespace}/{entry.digest}, read "
                    f"{int(now - entry.used)}s ago; the store is over its budget "
                    "with trees read within the keep window"
                )
            total -= freed
        if total > self.budget_bytes:
            log(
                f"{self.store} holds {total} bytes, over its budget of "
                f"{self.budget_bytes}: the rest is held by running actions, is in a "
                "trash it could not empty, or is in a namespace that takes no holds"
            )

    def _sweep(
        self,
        namespace: Namespace,
        found: Scanned,
        processes: Processes,
        sizes: dict[tuple[str, str], int],
        entries: list[Entry],
    ) -> int:
        """Drop the namespace's dead holds and reclaim its orphans, judged by
        a listing read after `found`; the bytes it still holds, with each
        published entry added to `entries`."""
        assert namespace.holds is not None
        total = 0
        held: set[str] = set()
        for name, held_object, pid, started in found.holds:
            if processes.gone(pid, started):
                namespace.holds.unlink(name)
            else:
                held.add(held_object)
        published = self._published(namespace)
        for name in found.objects:
            if name in published:
                total += self._size(namespace, name, sizes, cache=True)
                digest = published[name]
                used = self._used(namespace, digest)
                if used is not None:
                    entries.append(Entry(namespace.name, digest, name, used))
            elif name in held or not self._creator_gone(name, processes):
                total += self._size(namespace, name, sizes, cache=False)
            elif name in self._published(namespace):
                # Published by its creator, which has exited, since the
                # entries were read above.
                total += self._size(namespace, name, sizes, cache=True)
            else:
                size = self._size(namespace, name, sizes, cache=False)
                # Nothing new goes into a trash that could not drain, and
                # what could not be removed still takes its disk.
                if not (
                    self._draining
                    and self._discard_object(
                        namespace, name, "unpublished, held by nothing", size
                    )
                ):
                    total += size
        return total

    @staticmethod
    def _drop_unnamed_use_records(namespace: Namespace) -> None:
        """Drop each use record whose digest no entry names. A publication
        between the check and the unlink loses only its first use time, and
        its link's own time stands in for it, as in _evict."""
        if namespace.used is None:
            return
        for digest in namespace.used.names():
            if not namespace.directory.is_symlink(digest):
                namespace.used.unlink(digest)

    def _retire(self, namespace: Namespace) -> None:
        """Rename the namespace into the trash, and delete it there, if
        nothing is in it; a marker an interrupted pass left goes otherwise."""
        directory = namespace.directory
        if not (self._draining and self._empty(namespace)):
            directory.unlink(_RETIRING)
            return
        os.close(
            os.open(_RETIRING, os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=directory.fd)
        )
        # Read after the marker: a helper that claimed the namespace before it
        # could see the marker holds it by now (archive_mount.sh).
        if not self._empty(namespace):
            directory.unlink(_RETIRING)
            return
        self._discard(
            self._store,
            namespace.name,
            namespace.name,
            f"retired the empty namespace {namespace.name}",
        )

    @staticmethod
    def _empty(namespace: Namespace) -> bool:
        """No object, entry or hold is in the namespace, which its helpers
        claim. Use records go with it: a helper writes one only while it holds
        the namespace. One missing a part or the claims marker stays."""
        if (
            namespace.objects is None
            or namespace.holds is None
            or namespace.used is None
        ):
            return False
        names = set(namespace.directory.names())
        if _CLAIMS not in names or names - {*_PARTS, _RETIRING, _CLAIMS}:
            return False
        return not (namespace.objects.names() or namespace.holds.names())

    def _namespace_names(self) -> list[str]:
        return [name for name in self._store.names() if _NAMESPACE.fullmatch(name)]

    @contextlib.contextmanager
    def _namespace(self, name: str) -> Iterator[Namespace | None]:
        """The namespace `name`, opened from the store this pass locked, with
        its descriptors closed when the block ends; None when it has gone."""
        with contextlib.ExitStack() as opened:
            directory = self._store.child(name)
            if directory is None:
                yield None
                return
            opened.callback(directory.close)
            parts: dict[str, Directory | None] = {}
            for part in ("objects", "holds", "used"):
                child = directory.child(part)
                if child is not None:
                    opened.callback(child.close)
                parts[part] = child
            yield Namespace(directory, parts["objects"], parts["holds"], parts["used"])

    def _foreign(self, namespace: Namespace) -> int:
        """The bytes of a namespace whose readers take no holds, which this
        leaves alone: nothing would show which of its trees an action reads."""
        objects = self._objects(namespace)
        if objects:
            log(
                f"{namespace.name} takes no holds, so it is left alone; no helper "
                "that reads this store should have made it"
            )
        if namespace.objects is None:
            return 0
        total = 0
        for name in objects:
            try:
                total += tree_bytes(name, namespace.objects.fd)
            except FileNotFoundError:
                continue
        return total

    @staticmethod
    def _objects(namespace: Namespace) -> list[str]:
        return [] if namespace.objects is None else namespace.objects.names()

    @staticmethod
    def _published(namespace: Namespace) -> dict[str, str]:
        """Each published object's name, mapped to the digest naming it."""
        published: dict[str, str] = {}
        for name in namespace.directory.names():
            if not (_DIGEST.fullmatch(name) and namespace.directory.is_symlink(name)):
                continue
            target = namespace.directory.readlink(name)
            if target is not None and target.startswith("objects/"):
                published[target.removeprefix("objects/")] = name
        return published

    @staticmethod
    def _holds(namespace: Namespace) -> Iterator[tuple[str, str, int, str]]:
        if namespace.holds is None:
            return
        for name in namespace.holds.names():
            match = _HOLD.fullmatch(name)
            if match is not None:
                yield name, match["object"], int(match["pid"]), match["started"]

    def _held(self, namespace: Namespace, name: str) -> bool:
        """Whether a live process holds the object, from a reading of its holds
        and then of the processes, taken now."""
        holds = [hold for hold in self._holds(namespace) if hold[1] == name]
        if not holds:
            return False
        processes = self._reading()
        return any(not processes.gone(pid, started) for _, _, pid, started in holds)

    @staticmethod
    def _creator_gone(name: str, processes: Processes) -> bool:
        match = _OBJECT.fullmatch(name)
        # A name that identifies no creator may be a tree still being staged.
        return match is not None and processes.gone(int(match["pid"]), match["started"])

    @staticmethod
    def _used(namespace: Namespace, digest: str) -> float | None:
        """The entry's last use; its link's own time when no use is recorded,
        and None when the entry has gone."""
        if namespace.used is not None:
            recorded = namespace.used.mtime(digest)
            if recorded is not None:
                return recorded
        return namespace.directory.mtime(digest)

    def _size(
        self,
        namespace: Namespace,
        name: str,
        sizes: dict[tuple[str, str], int],
        *,
        cache: bool,
    ) -> int:
        key = (namespace.name, name)
        if cache and key in sizes:
            self._sizes[key] = sizes[key]
            return sizes[key]
        if namespace.objects is None:
            return 0
        try:
            size = tree_bytes(name, namespace.objects.fd)
        except FileNotFoundError:
            # A losing expander removes its own staging tree, and may do so
            # while it is read here.
            return 0
        if cache:
            self._sizes[key] = size
        return size

    def _evict(self, namespace: Namespace, entry: Entry, now: float) -> int:
        """Unpublish an entry and remove its object unless something holds it;
        the bytes freed."""
        if self._held(namespace, entry.object):
            return 0
        # Read since the pass listed it: in use again, however long it went
        # unused before. A read that lands after this costs that reader a
        # fresh expansion next time, never the tree it is reading, which its
        # hold keeps.
        if self._used(namespace, entry.digest) != entry.used:
            return 0
        if namespace.directory.readlink(entry.digest) != f"objects/{entry.object}":
            return 0
        namespace.directory.unlink(entry.digest)
        # Read after the unpublish: a read that relied on the entry took its
        # hold before it re-read the entry, so it is visible now.
        if self._held(namespace, entry.object):
            log(
                f"unpublished {namespace.name}/{entry.digest}; "
                f"{entry.object} is held, so a later pass removes it"
            )
            return 0
        size = self._sizes.get((namespace.name, entry.object), 0)
        age = int(now - entry.used)
        if not self._discard_object(
            namespace, entry.object, f"unused for {age}s", size
        ):
            return 0
        # The digest's use record goes too, unless an entry names it again. A
        # publication between the check and the unlink loses only its first
        # use time, and its link's own time stands in for it.
        if namespace.used is not None and not namespace.directory.is_symlink(
            entry.digest
        ):
            namespace.used.unlink(entry.digest)
        return size

    def _trash_directory(self) -> Directory:
        if self._trash is None:
            with contextlib.suppress(FileExistsError):
                os.mkdir(_TRASH, 0o700, dir_fd=self._store.fd)
            trash = self._store.child(_TRASH)
            if trash is None:
                raise NotADirectoryError(f"{self.store / _TRASH} is not a directory")
            self._opened.callback(trash.close)
            self._trash = trash
        return self._trash

    def _discard_object(
        self, namespace: Namespace, name: str, reason: str, size: int
    ) -> bool:
        """Rename an object into the trash, then delete it there; whether its
        `size` bytes are gone."""
        assert namespace.objects is not None
        if not self._discard(
            namespace.objects,
            name,
            f"{namespace.name}.{name}",
            f"evicted {namespace.name}/{name} ({reason}, {size} bytes)",
        ):
            return False
        self._sizes.pop((namespace.name, name), None)
        return True

    def _discard(
        self, parent: Directory, name: str, destination: str, done: str
    ) -> bool:
        """Rename the tree `name` in `parent` into the trash as `destination`,
        logging `done`, then delete it there; whether it is gone."""
        try:
            trash = self._trash_directory()
            # Moving a directory to another parent rewrites its `..`, which
            # Linux permits its owner only when the directory is writable.
            mode = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
            os.chmod(name, mode.st_mode | stat.S_IWUSR, dir_fd=parent.fd)
            os.rename(name, destination, src_dir_fd=parent.fd, dst_dir_fd=trash.fd)
        except OSError as error:
            log(
                f"cannot move {parent.label}/{name} into the trash ({error}); keeping it"
            )
            self._reclaimed = False
            return False
        log(done)
        if not self._delete_one(destination, trash):
            # Out of every reader's reach, but its bytes are still on disk.
            self._draining = False
            return False
        return True

    def _empty_trash(self, opened: contextlib.ExitStack) -> tuple[int, bool]:
        """Delete what the trash holds; the bytes left in it, and whether it
        emptied."""
        trash = self._store.child(_TRASH)
        if trash is None:
            return 0, True
        opened.callback(trash.close)
        self._trash = trash
        left, emptied = 0, True
        for name in trash.names():
            if not self._delete_one(name, trash):
                emptied = False
                try:
                    left += tree_bytes(name, trash.fd)
                except FileNotFoundError:
                    continue
        return left, emptied

    def _delete_one(self, name: str, trash: Directory) -> bool:
        try:
            self._delete(name, trash.fd)
        except OSError as error:
            # The tree is already out of every reader's reach; the next pass
            # finishes it.
            log(
                f"cannot finish deleting {trash.label}/{name} ({error}); "
                "the next pass retries"
            )
            self._reclaimed = False
            return False
        return True


def _non_negative_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"{text} is negative")
    return value


def _non_negative_seconds(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(f"{text} is not a finite, non-negative time")
    return value


def _positive_seconds(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"{text} is not a finite, positive time")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evict least recently used expansions from an archive store."
    )
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--budget-bytes", type=_non_negative_int, required=True)
    parser.add_argument("--keep-seconds", type=_non_negative_seconds, required=True)
    parser.add_argument(
        "--interval",
        type=_positive_seconds,
        help="run a pass this many seconds apart, forever; one pass without it",
    )
    args = parser.parse_args(argv)
    evictor = Evictor(args.store, args.budget_bytes, args.keep_seconds)
    if args.interval is None:
        return 0 if evictor.run_pass(time.time()) else 1
    failed = 0
    while True:
        try:
            completed = evictor.run_pass(time.time())
        except OSError as error:
            # A transient disk error: this pass is lost, and the next starts
            # over.
            log(f"pass failed: {error!r}")
            completed = False
        failed = 0 if completed else failed + 1
        if failed >= _FAILED_PASSES_BEFORE_EXIT:
            log(f"{failed} passes in a row failed; exiting so the failure is seen")
            return 1
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
