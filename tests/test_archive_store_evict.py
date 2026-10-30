"""archive_store_evict.py must free the store's least recently used trees, and
never one a running action reads or a reader could see half-deleted.

Entries are published by the real helper, archive_mount.sh, so its holds and
staging names are the ones the evictor reads on a worker. Use times are set
explicitly and every pass is given its own clock reading, so no verdict depends
on when the suite runs.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import gzip
import hashlib
import io
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import unittest.mock
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import Any, override

import archive_parts

INFINITE: float = import_module("unittest_timeouts").INFINITE
evict = import_module("archive_store_evict")

_HELPER = Path(__file__).parent.parent / "archive_store" / "archive_mount.sh"
_EVICTOR = Path(__file__).parent.parent / "archive_store" / "archive_store_evict.py"
_NAMESPACE = hashlib.sha256(_HELPER.read_bytes()).hexdigest()[:16]
# Every pass's clock reading, and the keep window measured back from it.
_NOW = 2_000_000_000.0
_KEEP = 3_600.0
_LONG_UNUSED = _NOW - 10 * _KEEP


def _tar_bytes(payload: bytes) -> bytes:
    """A tar of `payload` whose bytes depend on nothing else, so one payload is
    one digest."""
    buffer = io.BytesIO()
    # gzip records a time in its header unless told one.
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        info = tarfile.TarInfo("tool")
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        archive.addfile(info)
        info = tarfile.TarInfo("tool/data.txt")
        info.size = len(payload)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _tree_bytes(path: Path) -> int:
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        result: int = evict.tree_bytes(path.name, parent)
        return result
    finally:
        os.close(parent)


def _dead_pid() -> int:
    """The PID of a process that has exited."""
    child = subprocess.Popen(["true"])
    child.wait(timeout=INFINITE)
    return child.pid


class ArchiveStoreEvictTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self._remove, scratch)
        self.root = Path(scratch.name)
        self.store = self.root / "store"
        self.store.mkdir()
        self.namespace = self.store / _NAMESPACE

    def _remove(self, scratch: tempfile.TemporaryDirectory[str]) -> None:
        # Expansions are read-only; the scratch directory cannot be removed
        # until they are writable again.
        for directory, names, _ in os.walk(scratch.name):
            for name in names:
                path = Path(directory, name)
                if not path.is_symlink():
                    path.chmod(0o755)
        scratch.cleanup()

    def _reader(
        self, name: str, payload: bytes, command: list[str]
    ) -> tuple[subprocess.Popen[bytes], str]:
        """Start an action that mounts an archive of `payload` and runs
        `command`, and the archive's digest."""
        content = _tar_bytes(payload)
        digest = hashlib.sha256(content).hexdigest()
        workdir = self.root / name
        workdir.mkdir()
        # As the conversion writes a tar: a directory of its parts.
        (workdir / "tool.parts").mkdir()
        part = archive_parts.part_name(archive_parts.TAR_NAME, 0)
        (workdir / "tool.parts" / part).write_bytes(content)
        (workdir / "manifest").write_text(
            f"tool|{digest}|tool.parts|tool\n", encoding="ascii"
        )
        process = subprocess.Popen(
            ["/bin/sh", str(_HELPER), "manifest", "--", *command],
            cwd=workdir,
            env={
                "PATH": "/usr/bin:/bin",
                "TMPDIR": str(self.root),
                "RULES_ARCHIVE_STORE_DIR": str(self.store),
            },
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return process, digest

    def _publish(self, name: str, payload: bytes, used: float) -> str:
        """Run an action that reads an archive and exits, leaving its entry
        published, last used at `used`."""
        process, digest = self._reader(name, payload, ["cat", "tool/data.txt"])
        stdout, stderr = process.communicate(timeout=INFINITE)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout, payload)
        self._use(digest, used)
        return digest

    def _use(self, digest: str, used: float) -> None:
        os.utime(self.namespace / "used" / digest, (used, used))

    def _object(self, digest: str) -> Path:
        return self.namespace / os.readlink(self.namespace / digest)

    def _objects(self) -> list[str]:
        return sorted(path.name for path in (self.namespace / "objects").iterdir())

    def _trash(self) -> list[str]:
        trash = self.store / ".trash"
        return sorted(path.name for path in trash.iterdir()) if trash.is_dir() else []

    def _pass(
        self,
        budget: int,
        delete: Callable[[str, int], None] = evict.delete_tree,
        processes: Callable[[], Any] = evict.list_processes,
        evictor: Any = None,
    ) -> str:
        """Run one pass at _NOW and return what it logged."""
        if evictor is None:
            evictor = evict.Evictor(
                self.store, budget, _KEEP, delete=delete, processes=processes
            )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            evictor.run_pass(_NOW)
        return stderr.getvalue()

    def _staging(self, name: str, identity: str, payload: bytes) -> Path:
        """A read-only tree under `objects`, as the helper stages one."""
        path = self.namespace / "objects" / f"{'1' * 64}.{identity}.{name}"
        (path / "tool").mkdir(parents=True)
        (path / "tool" / "data.bin").write_bytes(payload)
        (path / "tool").chmod(0o555)
        path.chmod(0o555)
        return path

    def _own_identity(self) -> str:
        processes = evict.list_processes()
        assert processes is not None
        return f"{os.getpid()}-{processes.started[os.getpid()]}"

    def test_least_recently_used_entries_go_first_until_under_budget(self) -> None:
        oldest = self._publish("a", b"oldest\n", _LONG_UNUSED - 300)
        middle = self._publish("b", b"middle\n", _LONG_UNUSED - 200)
        newest = self._publish("c", b"newest\n", _LONG_UNUSED - 100)
        kept = self._object(newest)
        budget = _tree_bytes(kept)
        self._pass(budget)
        self.assertFalse((self.namespace / oldest).is_symlink())
        self.assertFalse((self.namespace / middle).is_symlink())
        self.assertEqual(self._object(newest), kept)
        self.assertEqual(self._objects(), [kept.name])
        self.assertEqual(self._trash(), [])

    def test_over_budget_a_recently_read_tree_goes_and_is_logged_as_thrash(
        self,
    ) -> None:
        # The budget is a ceiling on the guest's disk, so a recent read does not
        # keep a tree once the store is over it; that it had to go is the sign
        # the budget is smaller than what the worker's actions read.
        digest = self._publish("a", b"recent\n", _NOW - _KEEP / 2)
        logged = self._pass(0)
        self.assertFalse((self.namespace / digest).is_symlink())
        self.assertEqual(self._objects(), [])
        self.assertIn("thrash", logged)

    def test_a_store_whose_every_tree_is_held_stays_over_budget_and_says_so(
        self,
    ) -> None:
        # A hold is the one thing the ceiling yields to: removing a tree an
        # action reads would fail that action.
        reader, digest = self._reader("a", b"held\n", ["cat"])
        mount = self.root / "a" / "tool"
        deadline = time.monotonic() + INFINITE
        while not mount.is_symlink():
            self.assertLess(time.monotonic(), deadline, "the reader never mounted")
            self.assertIsNone(reader.poll(), reader.stderr)
            time.sleep(0.05)
        self._use(digest, _LONG_UNUSED)
        logged = self._pass(0)
        self.assertTrue(self._object(digest).is_dir())
        self.assertIn("held by running actions", logged)
        _, stderr = reader.communicate(timeout=INFINITE)
        self.assertEqual(reader.returncode, 0, stderr)

    def test_an_entry_read_since_the_pass_listed_it_is_passed_over(self) -> None:
        # The pass orders entries by a use time read when it listed them; one
        # read again since is no longer the least recently used, even though
        # its reader has already exited and left no live hold.
        stale = self._publish("a", b"read again\n", _LONG_UNUSED - 100)
        other = self._publish("b", b"older read\n", _LONG_UNUSED)
        namespace = self.namespace
        use = self._use

        class Reread(evict.Evictor):  # type: ignore[misc, name-defined]
            def _held(self, held_namespace: Path, name: str) -> bool:
                if name == (namespace / os.readlink(namespace / stale)).name:
                    use(stale, _NOW)
                result: bool = super()._held(held_namespace, name)
                return result

        budget = _tree_bytes(self._object(other))
        self._pass(0, evictor=Reread(self.store, budget, _KEEP))
        self.assertTrue((self.namespace / stale).is_symlink())
        self.assertFalse((self.namespace / other).is_symlink())

    def test_a_trash_that_cannot_drain_takes_nothing_more_and_counts(self) -> None:
        # Bytes the evictor could not delete are still on the guest's disk, so
        # they must not read as freed, and further trees must not pile up
        # behind them in the trash.
        first = self._publish("a", b"first\n", _LONG_UNUSED - 100)
        second = self._publish("b", b"second\n", _LONG_UNUSED)

        def failing(name: str, parent: int) -> None:
            raise OSError("disk error")

        for _ in range(2):
            logged = self._pass(0, delete=failing)
            self.assertEqual(len(self._trash()), 1)
            self.assertFalse((self.namespace / first).is_symlink())
            self.assertTrue((self.namespace / second).is_symlink())
            self.assertIn("over its budget", logged)

    def test_a_held_entry_survives_until_its_reader_exits(self) -> None:
        # The tree a running action reads must outlive any eviction, however
        # long ago its entry was last used: removing it would fail the action
        # partway through.
        reader, digest = self._reader("a", b"held\n", ["cat"])
        mount = self.root / "a" / "tool"
        deadline = time.monotonic() + INFINITE
        while not mount.is_symlink():
            self.assertLess(time.monotonic(), deadline, "the reader never mounted")
            self.assertIsNone(reader.poll(), reader.stderr)
            time.sleep(0.05)
        self._use(digest, _LONG_UNUSED)
        held = self._object(digest)
        self._pass(0)
        self.assertEqual(self._object(digest), held)
        self.assertTrue((mount / "data.txt").is_file())
        _, stderr = reader.communicate(timeout=INFINITE)
        self.assertEqual(reader.returncode, 0, stderr)
        self._pass(0)
        self.assertFalse((self.namespace / digest).is_symlink())
        self.assertEqual(self._objects(), [])
        # Its hold goes with its process.
        self.assertEqual(list((self.namespace / "holds").iterdir()), [])

    def test_an_interrupted_delete_resumes_and_no_reader_sees_the_half_tree(
        self,
    ) -> None:
        # The tree is renamed out of reach before any of it is deleted, so a
        # delete cut short leaves a partial tree only in the trash, which the
        # next pass finishes and no reader can reach.
        payload = b"whole\n"
        digest = self._publish("a", payload, _LONG_UNUSED)

        def interrupted(name: str, parent: int) -> None:
            for _, _, _, fd in os.fwalk(name, dir_fd=parent):
                os.fchmod(fd, 0o700)
            os.unlink(f"{name}/tool/data.txt", dir_fd=parent)
            raise OSError("interrupted")

        logged = self._pass(0, delete=interrupted)
        self.assertIn("the next pass retries", logged)
        self.assertFalse((self.namespace / digest).is_symlink())
        self.assertEqual(self._objects(), [])
        [partial] = self._trash()
        self.assertFalse((self.store / ".trash" / partial / "tool/data.txt").exists())
        # A reader arriving now expands the archive afresh and reads it whole.
        reader, again = self._reader("b", payload, ["cat", "tool/data.txt"])
        stdout, stderr = reader.communicate(timeout=INFINITE)
        self.assertEqual((reader.returncode, again), (0, digest), stderr)
        self.assertEqual(stdout, payload)
        self._pass(1 << 40)
        self.assertEqual(self._trash(), [])
        self.assertTrue((self._object(digest) / "tool/data.txt").is_file())

    def test_a_killed_expansion_is_removed_and_one_still_staging_is_kept(
        self,
    ) -> None:
        # An expansion killed before it published leaves a tree no entry names;
        # one whose creator still runs may be mid-extraction, and one whose
        # name identifies no creator might be, so neither is removed.
        self._publish("a", b"present\n", _NOW)
        digest = "0" * 64
        processes = evict.list_processes()
        assert processes is not None
        started = processes.started[os.getpid()]
        objects = self.namespace / "objects"
        killed = objects / f"{digest}.{_dead_pid()}-SunJan1000000002000.aaaaaa"
        staging = objects / f"{digest}.{os.getpid()}-{started}.bbbbbb"
        unknown = objects / f"{digest}.cccccc"
        for path in (killed, staging, unknown):
            (path / "tool").mkdir(parents=True)
            (path / "tool").chmod(0o555)
            path.chmod(0o555)
        self._pass(1 << 40)
        names = self._objects()
        self.assertNotIn(killed.name, names)
        self.assertIn(staging.name, names)
        self.assertIn(unknown.name, names)
        self.assertEqual(self._trash(), [])

    def test_a_hold_whose_pid_was_reused_keeps_nothing(self) -> None:
        # A PID alone is no identity: a hold naming a live PID with another
        # start time belongs to a process that has gone.
        digest = self._publish("a", b"reused\n", _LONG_UNUSED)
        name = self._object(digest).name
        (self.namespace / "holds" / f"{name}@{os.getpid()}-SunJan1000000002000").touch()
        self._pass(0)
        self.assertFalse((self.namespace / digest).is_symlink())
        self.assertEqual(self._objects(), [])
        self.assertEqual(list((self.namespace / "holds").iterdir()), [])

    def test_a_pass_skips_while_another_evictor_holds_the_lock(self) -> None:
        digest = self._publish("a", b"locked\n", _LONG_UNUSED)
        lock = os.open(self.store / ".evict.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        logged = self._pass(0)
        self.assertIn("skipping this pass", logged)
        self.assertTrue(self._object(digest).is_dir())

    def test_a_staging_trees_size_is_read_again_once_it_publishes(self) -> None:
        # A tree still being staged grows until it publishes; a size read while
        # it was small and kept across passes would undercount the store for as
        # long as the evictor ran, and the budget would never bind.
        kept = self._object(self._publish("a", b"kept\n", _NOW))
        staging = self._staging("growing", self._own_identity(), b"")
        evictor = evict.Evictor(self.store, 1 << 40, _KEEP)
        self._pass(0, evictor=evictor)
        small = _tree_bytes(staging)
        staging.chmod(0o755)
        (staging / "tool").chmod(0o755)
        (staging / "tool" / "data.bin").write_bytes(b"x" * (1 << 20))
        (staging / "tool").chmod(0o555)
        staging.chmod(0o555)
        digest = "1" * 64
        (self.namespace / digest).symlink_to(f"objects/{staging.name}")
        (self.namespace / "used" / digest).touch()
        self._use(digest, _LONG_UNUSED)
        # Exactly what the store holds if the grown tree still counted small.
        evictor.budget_bytes = _tree_bytes(kept) + small
        self._pass(0, evictor=evictor)
        self.assertFalse((self.namespace / digest).is_symlink())
        self.assertTrue(kept.is_dir())

    def test_an_object_published_after_the_entries_were_read_is_not_removed(
        self,
    ) -> None:
        # An expander can publish and exit between the evictor's read of the
        # entries and its check of the creator; removing the object then would
        # leave a published link naming nothing, which every later read of the
        # archive would mount.
        digest = self._publish("a", b"raced\n", _NOW)
        published = self._object(digest)

        class Raced(evict.Evictor):  # type: ignore[misc, name-defined]
            first = True

            @staticmethod
            def _published(namespace: Path) -> dict[str, str]:
                if Raced.first:
                    Raced.first = False
                    return {}
                result: dict[str, str] = evict.Evictor._published(namespace)
                return result

        self._pass(1 << 40, evictor=Raced(self.store, 1 << 40, _KEEP))
        self.assertEqual(self._object(digest), published)
        self.assertTrue((published / "tool/data.txt").is_file())

    def test_a_namespace_that_takes_no_holds_is_left_alone(self) -> None:
        # A helper revision from before holds leaves nothing that shows whether
        # its actions still read a tree. It reads a store variable nothing sets,
        # so none should reach a worker's store; one that does is not evicted.
        digest = self._publish("a", b"old helper\n", _LONG_UNUSED)
        holds = self.namespace / "holds"
        for hold in holds.iterdir():
            hold.unlink()
        holds.rmdir()
        logged = self._pass(0)
        self.assertTrue(self._object(digest).is_dir())
        self.assertIn("takes no holds", logged)

    def test_an_evicted_entrys_use_record_goes_with_it(self) -> None:
        # One record per distinct digest a store ever served would otherwise
        # pile up beside the trees the budget bounds.
        for index in range(3):
            self._publish(f"a{index}", f"distinct {index}\n".encode(), _LONG_UNUSED)
        self._pass(0)
        self.assertEqual(self._objects(), [])
        self.assertEqual(list((self.namespace / "used").iterdir()), [])

    def test_a_use_record_an_unfinished_eviction_left_is_dropped(self) -> None:
        # An eviction whose delete is cut short has unpublished its entry but
        # kept its use record. Once a later pass empties the trash nothing
        # names that digest, and while another tree keeps the namespace in use,
        # every such eviction would leave one more record behind.
        evicted = self._publish("a", b"evicted\n", _LONG_UNUSED)
        kept = self._publish("b", b"kept\n", _NOW)
        budget = _tree_bytes(self._object(kept))

        def failing(name: str, parent: int) -> None:
            raise OSError("disk error")

        self._pass(budget, delete=failing)
        self.assertFalse((self.namespace / evicted).is_symlink())
        self.assertEqual(len(self._trash()), 1)
        self._pass(budget)
        self.assertEqual(self._trash(), [])
        used = sorted(path.name for path in (self.namespace / "used").iterdir())
        self.assertEqual(used, [kept])

    def test_killed_expansions_wait_while_the_trash_cannot_drain(self) -> None:
        # Removing killed expansions fills the same trash as eviction does, so
        # the same stop applies, and a tree that could not be removed still
        # counts its bytes.
        self._publish("a", b"present\n", _NOW)
        killed = [
            self._staging(f"k{index}", f"{_dead_pid()}-SunJan1000000002000", b"x")
            for index in range(3)
        ]

        def failing(name: str, parent: int) -> None:
            raise OSError("disk error")

        for _ in range(2):
            logged = self._pass(0, delete=failing)
            self.assertEqual(len(self._trash()), 1)
            self.assertEqual(sum(path.is_dir() for path in killed), len(killed) - 1)
            self.assertIn("over its budget", logged)

    def test_nothing_is_removed_on_the_word_of_a_listing_ps_could_not_make(
        self,
    ) -> None:
        # Without a reading of the processes, a dead hold and a live one look
        # alike, and so do a killed expansion and one still staging.
        digest = self._publish("a", b"unknown\n", _LONG_UNUSED)
        name = self._object(digest).name
        (self.namespace / "holds" / f"{name}@{_dead_pid()}-SunJan1000000002000").touch()
        killed = self._staging("killed", f"{_dead_pid()}-SunJan1000000002000", b"x")
        self._pass(0, processes=lambda: None)
        self.assertTrue(self._object(digest).is_dir())
        self.assertTrue(killed.is_dir())
        self.assertTrue(any((self.namespace / "holds").iterdir()))

    def test_a_pass_keeps_to_the_store_it_locked_when_a_start_replaces_it(
        self,
    ) -> None:
        # A worker start sets the store aside and makes a new one at the same
        # path while a pass may be running, and the new store soon holds an
        # entry for the same digest: a pass reaching the new store by name
        # would unpublish that entry and drop its use record. The pass works on
        # the store it locked, wherever that now is, and never on the new one.
        first = self._publish("a", b"first\n", _LONG_UNUSED - 100)
        second = self._publish("b", b"second\n", _LONG_UNUSED)
        aside = self.root / "aside"

        def replace_store(name: str, parent: int) -> None:
            evict.delete_tree(name, parent)
            if aside.exists():
                return
            self.store.rename(aside)
            namespace = self.store / _NAMESPACE
            (namespace / "objects" / "fresh").mkdir(parents=True)
            (namespace / "holds").mkdir()
            (namespace / "used").mkdir()
            for digest in (first, second):
                (namespace / digest).symlink_to("objects/fresh")
                (namespace / "used" / digest).touch()

        self._pass(0, delete=replace_store)
        new = self.store / _NAMESPACE
        for digest in (first, second):
            self.assertEqual(os.readlink(new / digest), "objects/fresh")
            self.assertTrue((new / "used" / digest).is_file())
        self.assertTrue((new / "objects" / "fresh").is_dir())
        # The store the pass locked was evicted through, set aside or not.
        old = aside / _NAMESPACE
        self.assertEqual([p for p in old.iterdir() if p.is_symlink()], [])
        self.assertEqual(list((old / "objects").iterdir()), [])

    def test_a_pass_holds_one_namespaces_descriptors_at_a_time_and_closes_all(
        self,
    ) -> None:
        # A store can hold many namespaces, one per helper revision; a pass
        # that held each one's directories open would run a long-lived evictor
        # out of descriptors. One whose eviction creates the trash must close
        # that too.
        digest = self._publish("a", b"evicted\n", _LONG_UNUSED)
        for index in range(40):
            namespace = self.store / f"{index:016x}"
            for part in ("objects", "holds", "used"):
                (namespace / part).mkdir(parents=True)
            (namespace / ".claims").touch()
        self.assertFalse((self.store / ".trash").exists())

        def open_descriptors() -> int:
            return len(os.listdir("/dev/fd"))

        baseline = open_descriptors()
        during: list[int] = []

        # Every retirement, and the eviction, deletes with its namespace open.
        def counting(name: str, parent: int) -> None:
            during.append(open_descriptors())
            evict.delete_tree(name, parent)

        self._pass(0, delete=counting)
        self.assertFalse((self.namespace / digest).is_symlink())
        self.assertGreater(len(during), 40)
        # The store, its lock, the trash and one namespace's directories;
        # never one set per namespace.
        self.assertLess(max(during) - baseline, 12)
        self.assertEqual(open_descriptors(), baseline)

    def test_emptied_namespaces_are_retired_and_those_in_use_stay(self) -> None:
        # A namespace whose trees have all gone would otherwise stay, with its
        # use records and holds, until the store is next emptied, and cost
        # every pass a read of it. One a running helper has claimed must stay,
        # however empty, or the helper stages into a namespace that is gone.
        digest = self._publish("a", b"in use\n", _NOW)
        # A marker a pass interrupted mid-retirement left behind.
        (self.namespace / ".retiring").touch()
        emptied = [self.store / f"{index:016x}" for index in range(3)]
        for namespace in emptied:
            for part in ("objects", "holds", "used"):
                (namespace / part).mkdir(parents=True)
            (namespace / ".claims").touch()
            (namespace / "used" / ("2" * 64)).touch()
            dead = f"namespace@{_dead_pid()}-SunJan1000000002000"
            (namespace / "holds" / dead).touch()
        claimed = self.store / ("f" * 16)
        for part in ("objects", "holds", "used"):
            (claimed / part).mkdir(parents=True)
        (claimed / ".claims").touch()
        # A namespace of a helper revision from before claims, whose helper
        # may stage in it with no hold the pass could see.
        unclaimed = self.store / ("e" * 16)
        for part in ("objects", "holds", "used"):
            (unclaimed / part).mkdir(parents=True)
        (claimed / "holds" / f"namespace@{self._own_identity()}").touch()
        listings: list[object] = []

        def listing() -> object:
            listings.append(None)
            return evict.list_processes()

        logged = self._pass(1 << 40, processes=listing)
        namespaces = sorted(
            path.name for path in self.store.iterdir() if path.name != ".trash"
        )
        self.assertEqual(
            namespaces,
            sorted([_NAMESPACE, claimed.name, unclaimed.name, ".evict.lock"]),
        )
        self.assertEqual(self._trash(), [])
        self.assertTrue(self._object(digest).is_dir())
        self.assertFalse((self.namespace / ".retiring").exists())
        self.assertIn("retired the empty namespace", logged)
        # One reading of the processes judges every namespace's holds.
        self.assertEqual(len(listings), 1)

    def test_a_helper_claiming_a_namespace_as_it_retires_stages_in_a_new_one(
        self,
    ) -> None:
        # A helper can make its namespace's directories just before an evictor
        # marks the namespace and reads it empty, and take its hold just after:
        # the evictor then renames the namespace away under it. The helper must
        # notice and claim the namespace afresh, not stage into one that is
        # gone or publish an entry naming a tree that went with it.
        for part in ("objects", "holds", "used"):
            (self.namespace / part).mkdir(parents=True)
        (self.namespace / ".claims").touch()
        payload = b"claimed late\n"
        started: list[tuple[subprocess.Popen[bytes], str]] = []
        namespace = self.namespace
        reader = self._reader

        class Raced(evict.Evictor):  # type: ignore[misc, name-defined]
            def _empty(self, checked: Any) -> bool:
                result: bool = evict.Evictor._empty(checked)
                if started or not (namespace / ".retiring").exists():
                    return result
                started.append(reader("a", payload, ["cat", "tool/data.txt"]))
                deadline = time.monotonic() + INFINITE
                while not any((namespace / "holds").glob("namespace@*")):
                    assert time.monotonic() < deadline, "the reader never claimed"
                    time.sleep(0.01)
                return result

        logged = self._pass(1 << 40, evictor=Raced(self.store, 1 << 40, _KEEP))
        self.assertIn("retired the empty namespace", logged)
        [(process, digest)] = started
        stdout, stderr = process.communicate(timeout=INFINITE)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout, payload)
        self.assertNotIn(b"private store", stderr)
        self.assertTrue((self._object(digest) / "tool/data.txt").is_file())
        self.assertEqual(self._trash(), [])

    def test_a_failure_reading_an_entry_keeps_the_entry_and_its_object(
        self,
    ) -> None:
        # Reading a published entry as absent would leave its object to be
        # removed as unpublished; only a gone entry or a non-link reads so, and
        # any other failure stops the pass.
        digest = self._publish("a", b"unreadable\n", _LONG_UNUSED)
        published = self._object(digest)
        real = os.readlink

        def failing(path: str, *, dir_fd: int | None = None) -> str:
            if path == digest:
                raise OSError(errno.EIO, "input/output error")
            return real(path, dir_fd=dir_fd)

        with (
            unittest.mock.patch.object(evict.os, "readlink", failing),
            self.assertRaises(OSError),
        ):
            self._pass(0)
        self.assertEqual(self._object(digest), published)
        self.assertTrue((published / "tool/data.txt").is_file())

    def test_the_daemon_exits_with_an_error_after_passes_keep_failing(self) -> None:
        # A pass that keeps failing removes nothing; logging that forever while
        # the store grows would hide it, so after a few in a row the daemon
        # exits with an error for its service manager to record. A lock path
        # that is a directory fails every pass at its first open.
        self._publish("a", b"present\n", _NOW)
        (self.store / ".evict.lock").mkdir()
        result = subprocess.run(
            [
                sys.executable,
                "-E",
                "-s",
                str(_EVICTOR),
                "--store",
                str(self.store),
                "--budget-bytes",
                "0",
                "--keep-seconds",
                "0",
                "--interval",
                "0.01",
            ],
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=False,
            timeout=INFINITE,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("passes in a row failed", result.stderr)

    def test_a_tree_that_will_not_delete_fails_every_pass_it_stays(self) -> None:
        # A tree the evictor cannot remove is the failure that leaves the store
        # growing, so each pass it stays counts as failed, which is what brings
        # the daemon to exit with an error (the test above shows that exit).
        self._publish("a", b"stuck\n", _LONG_UNUSED)
        stuck = OSError(errno.EPERM, "operation not permitted")
        evictor = evict.Evictor(self.store, 0, _KEEP)
        with (
            unittest.mock.patch.object(evict.shutil, "rmtree", side_effect=stuck),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            # The pass that moves the tree into the trash, then those that
            # find it still there.
            completed = [evictor.run_pass(_NOW) for _ in range(3)]
        self.assertEqual(completed, [False, False, False])
        self.assertEqual(len(self._trash()), 1)
        # Once the tree goes, a pass completes again.
        self.assertTrue(evictor.run_pass(_NOW))
        self.assertEqual(self._trash(), [])

    def test_settings_that_would_evict_everything_or_spin_are_refused(self) -> None:
        store = str(self.store)
        for arguments in (
            ["--budget-bytes", "-1", "--keep-seconds", "0"],
            ["--budget-bytes", "0", "--keep-seconds", "-1"],
            ["--budget-bytes", "0", "--keep-seconds", "nan"],
            ["--budget-bytes", "0", "--keep-seconds", "0", "--interval", "0"],
            ["--budget-bytes", "0", "--keep-seconds", "0", "--interval", "inf"],
        ):
            with (
                self.subTest(arguments),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                evict.main(["--store", store, *arguments])


if __name__ == "__main__":
    unittest.main()
