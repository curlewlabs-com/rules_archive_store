"""archive_mount.sh must publish each expansion exactly once, and only whole,
and keep a hold on every tree it mounts, against readers and evictors working
the same store at once.

These run the real helper through archive_mount_test_support's harness, and
hold it at its test seams while the store changes under it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
import unittest
from collections.abc import Callable
from importlib import import_module

from archive_mount_test_support import (
    INFINITE,
    NAMESPACE,
    ArchiveMountCase,
    tar_bytes,
)

process_group = import_module("process_group")

# archive_mount.sh's test seams, which hold it before it publishes, before it
# re-reads an entry it holds, and before it checks a namespace it claimed.
_PUBLISH_GATE = "ARCHIVE_MOUNT_TEST_PUBLISH_GATE"
_HOLD_GATE = "ARCHIVE_MOUNT_TEST_HOLD_GATE"
_CLAIM_GATE = "ARCHIVE_MOUNT_TEST_CLAIM_GATE"


class ArchiveMountStoreTest(ArchiveMountCase):
    def test_concurrent_expanders_publish_one_expansion(self) -> None:
        # The staging/link protocol exists for this: one instance's slots
        # asking for the same archive at once must all read a whole tree, and
        # leave one expansion rather than one each. The publish gate holds every
        # expander before it publishes, and is released only once each has made
        # its staging directory, which it does after finding nothing published,
        # so every run races all of them on the link.
        archive, digest = self._archive("tool.parts", tar_bytes())
        manifest = f"tool|{digest}|tool.parts|tool-1.0\n"
        gate = self.root / "gate"
        os.mkfifo(gate)
        # Held open for reading and writing so the release below cannot block on a
        # reader, and readers arriving after it still find their lines.
        gate_fd = os.open(gate, os.O_RDWR)
        self.addCleanup(os.close, gate_fd)
        expanders = 8
        actions = [
            self._action(
                f"action-{index}",
                manifest,
                archive,
                ["cat", "tool/share/data.txt"],
                env={_PUBLISH_GATE: str(gate)},
            )
            for index in range(expanders)
        ]
        deadline = time.monotonic() + INFINITE
        while len(self._objects()) < expanders:
            self.assertLess(time.monotonic(), deadline, "expanders never all staged")
            for action in actions:
                self.assertIsNone(action.poll(), action.stderr)
            time.sleep(0.05)
        os.write(gate_fd, b"\n" * expanders)
        losers = 0
        for action in actions:
            stdout, stderr = action.communicate(timeout=INFINITE)
            self.assertEqual(action.returncode, 0, stderr)
            self.assertEqual(stdout, b"payload\n")
            losers += b"discarded a duplicate expansion" in stderr
        self.assertEqual(losers, expanders - 1)
        self.assertEqual([path.name for path in self._published()], [digest])
        self.assertEqual(len(self._objects()), 1)
        self.assertTrue((self.store / NAMESPACE / "used" / digest).is_file())

    def _holds(self) -> list[str]:
        """The holds on objects; the helper's hold on its namespace, taken
        before any mount, is not one."""
        holds = self.store / NAMESPACE / "holds"
        if not holds.is_dir():
            return []
        return sorted(
            path.name
            for path in holds.iterdir()
            if not path.name.startswith("namespace@")
        )

    def test_a_read_holds_what_it_mounts_and_rereads_an_entry_evicted_under_it(
        self,
    ) -> None:
        # The evictor unpublishes an entry and only then reads the holds on its
        # object, so a reader must take its hold before checking the entry still
        # names that object. The hold gate stops the reader between the two, and
        # the test unpublishes the entry there, as an evictor would: the reader
        # must mount a fresh expansion, never the object it held, which an
        # evictor that read the holds before this one was taken may remove. The
        # hold on that object stays, since another mount of it could rely on it.
        archive, digest = self._archive("tool.parts", tar_bytes())
        gate = self.root / "hold-gate"
        os.mkfifo(gate)
        gate_fd = os.open(gate, os.O_RDWR)
        self.addCleanup(os.close, gate_fd)
        # `cat` waits on stdin, so the action's root process, which the helper
        # became by exec, is still alive when its hold is read below.
        action = self._action(
            "action",
            f"tool|{digest}|tool.parts|tool-1.0\n",
            archive,
            ["cat"],
            env={_HOLD_GATE: str(gate)},
        )
        deadline = time.monotonic() + INFINITE
        while not self._holds():
            self.assertLess(time.monotonic(), deadline, "the reader never held")
            self.assertIsNone(action.poll(), action.stderr)
            time.sleep(0.05)
        [first] = self._objects()
        (self.store / NAMESPACE / digest).unlink()
        os.write(gate_fd, b"\n")
        while len(self._objects()) < 2:
            self.assertLess(time.monotonic(), deadline, "the reader never re-expanded")
            self.assertIsNone(action.poll(), action.stderr)
            time.sleep(0.05)
        os.write(gate_fd, b"\n")
        mount = self.root / "action" / "tool"
        while not mount.is_symlink():
            self.assertLess(time.monotonic(), deadline, "the reader never mounted")
            self.assertIsNone(action.poll(), action.stderr)
            time.sleep(0.05)
        [entry] = self._published()
        second = (entry.parent / os.readlink(entry)).resolve()
        self.assertNotEqual(second, first.resolve())
        self.assertEqual(mount.resolve().parent, second)
        # A hold on the object it mounts, naming the live root process by the
        # identity the evictor will check it against. The hold on the object it
        # lost stays too: another mount of that object could rely on it.
        lstart = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(action.pid)],
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "TZ": "UTC0"},
            capture_output=True,
            check=True,
            timeout=INFINITE,
        ).stdout.decode()
        identity = f"{action.pid}-" + "".join(c for c in lstart if c.isalnum())
        self.assertEqual(
            self._holds(),
            sorted(f"{path.name}@{identity}" for path in (first, second)),
        )
        # Each staging name carries its creator, so the evictor can tell a
        # killed expansion from one still being staged.
        for path in self._objects():
            self.assertIn(f".{identity}.", path.name)
        _, stderr = action.communicate(timeout=INFINITE)
        self.assertEqual(action.returncode, 0, stderr)
        self.assertIn(b"unpublished while it was read", stderr)

    def test_a_second_mount_losing_the_entry_keeps_the_first_mounts_hold(
        self,
    ) -> None:
        # A hold is named for its object and its process, so every mount of
        # one archive in one action shares it. The second mount finding the entry
        # unpublished must not drop that hold, or the first mount's tree is
        # left for an evictor to remove while the action reads it.
        archive, digest = self._archive("tool.parts", tar_bytes())
        gate = self.root / "hold-gate"
        os.mkfifo(gate)
        gate_fd = os.open(gate, os.O_RDWR)
        self.addCleanup(os.close, gate_fd)
        action = self._action(
            "action",
            f"first|{digest}|tool.parts|tool-1.0\n"
            f"second|{digest}|tool.parts|tool-1.0/share\n",
            archive,
            ["cat"],
            env={_HOLD_GATE: str(gate)},
        )
        deadline = time.monotonic() + INFINITE

        def wait_for(condition: Callable[[], bool], what: str) -> None:
            while not condition():
                self.assertLess(time.monotonic(), deadline, what)
                self.assertIsNone(action.poll(), action.stderr)
                time.sleep(0.05)

        workdir = self.root / "action"
        wait_for(lambda: bool(self._holds()), "the first mount never held")
        [first] = self._objects()
        # The second mount's hold is the same file, so its arrival at the gate
        # shows only as that file touched again. Date it back while the first
        # mount waits at the gate, so any later time is the second mount's.
        [hold] = self._holds()
        hold_path = self.store / NAMESPACE / "holds" / hold
        os.utime(hold_path, (0, 0))
        os.write(gate_fd, b"\n")
        wait_for((workdir / "first").is_symlink, "the first mount never linked")
        wait_for(lambda: hold_path.stat().st_mtime > 0, "the second mount never held")
        # Unpublish the entry there, as an evictor would, before its re-read.
        (self.store / NAMESPACE / digest).unlink()
        os.write(gate_fd, b"\n")
        wait_for(
            lambda: len(self._objects()) == 2, "the second mount never re-expanded"
        )
        os.write(gate_fd, b"\n")
        wait_for((workdir / "second").is_symlink, "the second mount never linked")
        self.assertEqual((workdir / "first").resolve().parent, first.resolve())
        held = [name.split("@")[0] for name in self._holds()]
        self.assertIn(first.name, held)
        self.assertEqual(len(held), 2)
        _, stderr = action.communicate(timeout=INFINITE)
        self.assertEqual(action.returncode, 0, stderr)

    def test_each_mount_has_its_own_budget_of_lost_reads(self) -> None:
        # A read that loses its entry to an evictor tries again, a bounded
        # number of times. The bound is per mount: an action whose several
        # mounts each lose one read to separate evictions has lost no mount
        # entirely, and must not fail as if one had.
        names = ("one.parts", "two.parts", "three.parts")
        contents = (tar_bytes(), tar_bytes(link="run"), tar_bytes(up=True))
        archives = [
            self._archive(name, content)
            for name, content in zip(names, contents, strict=True)
        ]
        workdir = self.root / "action"
        workdir.mkdir()
        for path, _ in archives[1:]:
            (workdir / path.name).symlink_to(path)
        manifest = "".join(
            f"m{index}|{digest}|{path.name}|tool-1.0\n"
            for index, (path, digest) in enumerate(archives)
        )
        gate = self.root / "hold-gate"
        os.mkfifo(gate)
        gate_fd = os.open(gate, os.O_RDWR)
        self.addCleanup(os.close, gate_fd)
        action = self._action(
            "action", manifest, archives[0][0], ["true"], env={_HOLD_GATE: str(gate)}
        )
        deadline = time.monotonic() + INFINITE

        def wait_for_holds(count: int) -> None:
            while len(self._holds()) < count:
                self.assertLess(time.monotonic(), deadline, "a mount never held")
                self.assertIsNone(action.poll(), action.stderr)
                time.sleep(0.05)

        for index, (_, digest) in enumerate(archives):
            # The mount's first hold: unpublish its entry under it.
            wait_for_holds(2 * index + 1)
            (self.store / NAMESPACE / digest).unlink()
            os.write(gate_fd, b"\n")
            # Its second, on a fresh expansion: let it through.
            wait_for_holds(2 * index + 2)
            os.write(gate_fd, b"\n")
        _, stderr = action.communicate(timeout=INFINITE)
        self.assertEqual(action.returncode, 0, stderr)
        self.assertEqual(stderr.count(b"unpublished while it was read"), len(archives))

    def test_a_signal_before_publication_leaves_nothing(self) -> None:
        # A TERM while the expander holds a staged tree must remove it and
        # publish nothing, or the store would keep a tree no link names.
        archive, digest = self._archive("tool.parts", tar_bytes())
        gate = self.root / "gate"
        os.mkfifo(gate)
        gate_fd = os.open(gate, os.O_RDWR)
        self.addCleanup(os.close, gate_fd)
        action = self._action(
            "action",
            f"tool|{digest}|tool.parts|tool-1.0\n",
            archive,
            ["true"],
            env={_PUBLISH_GATE: str(gate)},
        )
        deadline = time.monotonic() + INFINITE
        while not self._objects():
            self.assertLess(time.monotonic(), deadline, "the expander never staged")
            time.sleep(0.05)
        self.assertTrue(process_group.signal_group(action.pid, signal.SIGTERM))
        # The shell runs its trap once the read it is blocked in returns.
        os.write(gate_fd, b"\n")
        action.communicate(timeout=INFINITE)
        self.assertNotEqual(action.returncode, 0)
        self.assertEqual(self._published(), [])
        self.assertEqual(self._objects(), [])

    def test_a_namespace_left_marked_retiring_is_done_without(self) -> None:
        # An evictor that dies mid-retirement leaves its marker until its next
        # pass; an action reading the namespace meanwhile expands into a store
        # of its own rather than fail, and publishes nothing in the shared one.
        namespace = self.store / NAMESPACE
        for part in ("objects", "holds", "used"):
            (namespace / part).mkdir(parents=True)
        (namespace / ".retiring").touch()
        archive, digest = self._archive("tool.parts", tar_bytes())
        result = self._run(
            "action",
            f"tool|{digest}|tool.parts|tool-1.0\n",
            archive,
            ["cat", "tool/share/data.txt"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"payload\n")
        self.assertIn(b"expanding into a private store", result.stderr)
        self.assertEqual(self._published(), [])
        self.assertEqual(list(self.root.glob("archive-store.*")), [])

    def test_a_namespace_retired_between_its_parts_is_claimed_again(self) -> None:
        # mkdir makes a namespace's parts one at a time, so a retirement
        # between them leaves the later parts, and then the helper's hold, in a
        # namespace missing the earlier ones. A claim must not pass there, or
        # the helper stages into an objects directory that is not there.
        archive, digest = self._archive("tool.parts", tar_bytes())
        gate = self.root / "gate"
        os.mkfifo(gate)
        gate_fd = os.open(gate, os.O_RDWR)
        self.addCleanup(os.close, gate_fd)
        action = self._action(
            "action",
            f"tool|{digest}|tool.parts|tool-1.0\n",
            archive,
            ["cat", "tool/share/data.txt"],
            env={_CLAIM_GATE: str(gate)},
        )
        namespace = self.store / NAMESPACE
        deadline = time.monotonic() + INFINITE
        while not (holds := list(namespace.glob("holds/namespace@*"))):
            self.assertLess(time.monotonic(), deadline, "the helper never claimed")
            self.assertIsNone(action.poll(), action.stderr)
            time.sleep(0.05)
        [hold] = holds
        namespace.rename(self.root / "retired")
        (namespace / "used").mkdir(parents=True)
        (namespace / "holds").mkdir()
        (namespace / "holds" / hold.name).touch()
        # One line for this claim, one for the claim it goes round to.
        os.write(gate_fd, b"\n\n")
        stdout, stderr = action.communicate(timeout=INFINITE)
        self.assertEqual(action.returncode, 0, stderr)
        self.assertEqual(stdout, b"payload\n")
        self.assertNotIn(b"private store", stderr)
        self.assertEqual([path.name for path in self._published()], [digest])


if __name__ == "__main__":
    unittest.main()
