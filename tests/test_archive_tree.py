"""archive_tree.ts must build a linked tree once per store, mount it after that
without reading a unit, expand only the units the store lacks, and publish the
tree as an entry the evictor holds to the budget but never takes from a reader.

These run the real helper under Bun against a store in a scratch directory,
and the real evictor over that store. Trees are built on macOS only, where
clonefile(2) clones a unit's directory in one call.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from importlib import import_module
from pathlib import Path
from typing import override

import archive_parts

INFINITE: float = import_module("unittest_timeouts").INFINITE
evict = import_module("archive_store_evict")

HELPER = (Path(__file__).parent.parent / "archive_store" / "archive_tree.ts").absolute()
# The store namespace the helper builds into: the prefix of its own digest and
# its manifest reader's, together.
NAMESPACE = hashlib.sha256(
    HELPER.read_bytes() + HELPER.with_name("archive_tree_manifest.ts").read_bytes()
).hexdigest()[:16]
# Every evictor pass's clock reading, after any use the suite records, as
# test_archive_store_evict.py's _NOW is.
_NOW = 2_000_000_000.0


def unit_tar(files: dict[str, bytes], links: dict[str, str] | None = None) -> bytes:
    """A unit's tar as declared_archive's conversion writes it: gzipped, its
    members rooted at the unit, with directories made by their files, and
    `links` as symlinks to their targets."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        directories = sorted(
            {str(parent) for name in files for parent in Path(name).parents} - {"."}
        )
        for name in directories:
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            archive.addfile(info)
        for name, content in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o755 if name.startswith("bin/") else 0o644
            archive.addfile(info, io.BytesIO(content))
        for name, target in sorted((links or {}).items()):
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)
    return gzip.compress(buffer.getvalue(), mtime=0)


@unittest.skipUnless(sys.platform == "darwin", "trees are built on macOS")
class ArchiveTreeTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self._remove, scratch)
        self.root = Path(scratch.name).resolve()
        self.store = self.root / "store"
        self.store.mkdir()
        self.namespace = self.store / NAMESPACE
        # The test target names its Bun in ARCHIVE_TREE_TEST_BUN; without one,
        # as run by hand, the suite takes Bun from PATH.
        bun = os.environ.get("ARCHIVE_TREE_TEST_BUN") or shutil.which("bun")
        assert bun is not None, "the suite runs the helper under the declared Bun"
        bun = os.path.abspath(bun)
        # The launcher's link on PATH names Bun's runfiles launcher, which
        # finds its runtime beside the path it is started by.
        self.bun = os.readlink(bun) if os.path.islink(bun) else bun
        self.a = self._archive("a", {"index.js": b"a\n", "bin/a": b"bin a\n"})
        self.b = self._archive("b", {"index.js": b"b\n"})

    def _remove(self, scratch: tempfile.TemporaryDirectory[str]) -> None:
        # Expansions and trees are read-only; the scratch directory cannot be
        # removed until they are writable again.
        for directory, names, _ in os.walk(scratch.name):
            for name in names:
                path = Path(directory, name)
                if not path.is_symlink():
                    path.chmod(0o755)
        scratch.cleanup()

    def _archive(
        self,
        name: str,
        files: dict[str, bytes],
        links: dict[str, str] | None = None,
    ) -> tuple[Path, str]:
        """The unit's tar as the conversion writes it, a directory of its one
        part, and the tar's digest."""
        content = unit_tar(files, links)
        path = self.root / f"{name}.parts"
        path.mkdir()
        (path / archive_parts.part_name(archive_parts.TAR_NAME, 0)).write_bytes(content)
        return path, hashlib.sha256(content).hexdigest()

    def _manifest(
        self, b: tuple[Path, str], links: dict[str, str] | None = None
    ) -> str:
        """A tree of `a`, with an edge to `b`, linked at the root with a bin."""
        units = {
            ".store/a@1/node_modules/a": self.a,
            ".store/b@1/node_modules/b": b,
        }
        links = {
            ".store/a@1/node_modules/b": "../../b@1/node_modules/b",
            "node_modules/a": "../.store/a@1/node_modules/a",
            "node_modules/.bin/a": "../a/bin/a",
            **(links or {}),
        }
        lines = ["mount|tree"]
        lines += [f"unit|{dest}|{d}|{p}|." for dest, (p, d) in units.items()]
        lines += [f"link|{at}|{target}" for at, target in links.items()]
        return "\n".join(lines) + "\n"

    def _action(
        self, name: str, manifest: str, command: list[str], *options: str
    ) -> subprocess.Popen[bytes]:
        workdir = self.root / name
        workdir.mkdir(exist_ok=True)
        (workdir / "manifest").write_text(manifest, encoding="ascii")
        return subprocess.Popen(
            [
                self.bun,
                "--no-install",
                str(HELPER),
                *options,
                "manifest",
                "--",
                *command,
            ],
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

    def _run(
        self, name: str, manifest: str, command: list[str], *options: str
    ) -> subprocess.CompletedProcess[bytes]:
        process = self._action(name, manifest, command, *options)
        stdout, stderr = process.communicate(timeout=INFINITE)
        return subprocess.CompletedProcess(
            process.args, process.returncode, stdout, stderr
        )

    def _read_through(
        self, name: str, manifest: str
    ) -> subprocess.CompletedProcess[bytes]:
        """An action reading a unit, a unit through its edge, and a bin link."""
        result = self._run(
            name,
            manifest,
            [
                "cat",
                "tree/node_modules/a/index.js",
                "tree/.store/a@1/node_modules/b/index.js",
                "tree/node_modules/.bin/a",
            ],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def _entries(self) -> set[str]:
        if not self.namespace.is_dir():
            return set()
        return {path.name for path in self.namespace.iterdir() if path.is_symlink()}

    def _objects(self) -> list[str]:
        objects = self.namespace / "objects"
        return (
            sorted(path.name for path in objects.iterdir()) if objects.is_dir() else []
        )

    def _held_by(self, pid: int) -> set[str]:
        """The objects a hold names for the process `pid`."""
        return {
            path.name.split("@")[0]
            for path in (self.namespace / "holds").iterdir()
            if f"@{pid}-" in path.name and not path.name.startswith("namespace@")
        }

    def _object_of(self, entry: str) -> str:
        return os.readlink(self.namespace / entry).removeprefix("objects/")

    def test_a_tree_is_built_once_then_mounted_without_reading_a_unit(self) -> None:
        # The design rests on this: an action whose tree is published pays one
        # link, not a mount per unit (docs/store.md sec."Linked trees").
        manifest = self._manifest(self.b)
        first = self._read_through("first", manifest)
        self.assertEqual(first.stdout, b"a\nb\nbin a\n")
        self.assertIn(b"built tree", first.stderr)
        [tree] = self._entries() - {self.a[1], self.b[1]}
        objects = self._objects()

        second = self._action(
            "second", manifest, ["cat", "tree/node_modules/a/index.js"]
        )
        stdout, stderr = second.communicate(timeout=INFINITE)
        self.assertEqual(second.returncode, 0, stderr)
        self.assertEqual(stdout, b"a\n")
        self.assertIn(b"mounted published tree", stderr)
        self.assertEqual(self._objects(), objects)
        self.assertEqual(self._held_by(second.pid), {self._object_of(tree)})
        # A unit is a real directory inside the tree, never a link to its
        # expansion, since Bun resolves a package's dependencies from its
        # realpath.
        unit = (self.root / "second/tree/node_modules/a").resolve()
        self.assertTrue(
            unit.is_relative_to(self.namespace / "objects" / self._object_of(tree))
        )

    def test_a_tree_differing_by_one_unit_expands_only_that_unit(self) -> None:
        # A bump rebuilds the trees that reach it; every unit they share with a
        # published tree must be reused, not expanded again.
        self._read_through("first", self._manifest(self.b))
        expanded = self._entries()
        bumped = self._archive("b2", {"index.js": b"b2\n"})
        second = self._action(
            "second",
            self._manifest(bumped),
            ["cat", "tree/.store/a@1/node_modules/b/index.js"],
        )
        stdout, stderr = second.communicate(timeout=INFINITE)
        self.assertEqual(second.returncode, 0, stderr)
        self.assertEqual(stdout, b"b2\n")
        self.assertIn(b"expanded 1 of 2 archives", stderr)
        [tree] = self._entries() - expanded - {bumped[1]}
        self.assertEqual(self._entries() - expanded, {tree, bumped[1]})
        self.assertEqual(
            self._held_by(second.pid),
            {self._object_of(entry) for entry in (self.a[1], bumped[1], tree)},
        )

    def test_concurrent_first_readers_build_the_tree_once(self) -> None:
        # The first actions on an instance often want one tree at once; each
        # expanding every unit and building the tree only to discard it cost
        # every one of them the slowest build, about 12 s against 1 s on a
        # loaded Mac worker.
        readers = [
            self._action(
                f"reader-{index}",
                self._manifest(self.b),
                ["cat", "tree/node_modules/a/index.js"],
            )
            for index in range(4)
        ]
        reports = []
        for reader in readers:
            stdout, stderr = reader.communicate(timeout=INFINITE)
            self.assertEqual(reader.returncode, 0, stderr)
            self.assertEqual(stdout, b"a\n")
            reports.append(stderr)
        self.assertEqual(sum(b"built tree" in report for report in reports), 1, reports)
        self.assertFalse(
            any(b"discarded a duplicate" in report for report in reports), reports
        )
        self.assertEqual(len(self._objects()), 3)

    def test_a_held_tree_outlives_eviction_until_its_reader_exits(self) -> None:
        # A tree is an entry like an expansion's, so the evictor holds it to the
        # budget; removing it under a running action would fail the action.
        reader = self._action("reader", self._manifest(self.b), ["cat"])
        mount = self.root / "reader" / "tree"
        deadline = time.monotonic() + INFINITE
        while not mount.is_symlink():
            self.assertLess(time.monotonic(), deadline, "the reader never mounted")
            self.assertIsNone(reader.poll(), reader.stderr)
            time.sleep(0.05)
        evictor = evict.Evictor(self.store, 0, 0)
        with contextlib.redirect_stderr(io.StringIO()):
            evictor.run_pass(_NOW)
        self.assertEqual((mount / "node_modules/.bin/a").read_bytes(), b"bin a\n")
        _, stderr = reader.communicate(timeout=INFINITE)
        self.assertEqual(reader.returncode, 0, stderr)
        with contextlib.redirect_stderr(io.StringIO()):
            evictor.run_pass(_NOW)
        self.assertEqual(self._entries(), set())
        self.assertEqual(self._objects(), [])

    def test_a_writable_mount_is_a_clone_the_published_tree_never_sees(self) -> None:
        # A consumer that writes into its tree must not change what the next
        # action reads.
        manifest = self._manifest(self.b)
        write = [
            "/bin/sh",
            "-c",
            "echo w >>tree/node_modules/a/new && cat tree/node_modules/a/new",
        ]
        result = self._run("writer", manifest, write, "--writable")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"w\n")
        self.assertFalse((self.root / "writer/tree").is_symlink())
        reader = self._run("reader", manifest, ["ls", "tree/node_modules/a/"])
        self.assertEqual(reader.stdout.split(), [b"bin", b"index.js"])

    def test_a_rerun_in_the_same_root_replaces_its_own_mount(self) -> None:
        # A retried action in a shared execution root finds what its earlier
        # run mounted: a writable clone must come back fresh rather than read as
        # a collision, and either kind of mount may follow the other.
        manifest = self._manifest(self.b)
        write = [
            "/bin/sh",
            "-c",
            "echo w >>tree/node_modules/a/new && cat tree/node_modules/a/new",
        ]
        for _ in range(2):
            result = self._run("action", manifest, write, "--writable")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b"w\n")
        result = self._run("action", manifest, ["ls", "tree/node_modules/a/"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), [b"bin", b"index.js"])
        result = self._run("action", manifest, write, "--writable")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_an_unmarked_directory_at_the_mount_is_a_collision(self) -> None:
        # Only a clone this helper marked is its own to remove.
        (self.root / "action" / "tree").mkdir(parents=True)
        (self.root / "action" / "tree" / "kept").write_text("kept")
        result = self._run("action", self._manifest(self.b), ["true"], "--writable")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"already exists in the input root", result.stderr)
        self.assertEqual((self.root / "action/tree/kept").read_text(), "kept")

    def test_a_marker_naming_another_directory_removes_nothing(self) -> None:
        # The marker vouches for one directory by its inode, so a marker left
        # beside a directory the helper did not clone, or naming another,
        # deletes nothing.
        unowned = self.root / "action" / "tree"
        unowned.mkdir(parents=True)
        (unowned / "kept").write_text("kept")
        other = self.root / "other"
        other.mkdir()
        (self.root / "action" / "tree.writable").write_text(
            f"{other.stat().st_dev}:{other.stat().st_ino}"
        )
        result = self._run("action", self._manifest(self.b), ["true"], "--writable")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"already exists in the input root", result.stderr)
        self.assertEqual((unowned / "kept").read_text(), "kept")

    def test_a_clone_a_killed_run_left_is_removed_by_the_next(self) -> None:
        # A run killed between cloning and renaming leaves a whole tree clone
        # beside the mount, which no eviction reaches; its marker names it, so
        # the next run removes it, and only it.
        action = self.root / "action"
        action.mkdir()
        abandoned = action / "tree.clone.0123456789ab"
        (abandoned / "node_modules").mkdir(parents=True)
        (action / "tree.writable").write_text(f"{abandoned.name}\n")
        other = action / "tree.clone.ba9876543210"
        other.mkdir()
        result = self._run("action", self._manifest(self.b), ["true"], "--writable")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(abandoned.exists())
        self.assertTrue(other.is_dir())
        self.assertTrue((action / "tree" / "node_modules" / "a").is_dir())

    def test_a_marker_naming_a_path_outside_its_clones_removes_nothing(self) -> None:
        # Only a name the helper itself gives a staging clone is honored.
        action = self.root / "action"
        action.mkdir()
        kept = action / "kept"
        kept.mkdir()
        (action / "tree.writable").write_text("kept\n")
        result = self._run("action", self._manifest(self.b), ["true"], "--writable")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(kept.is_dir())

    def test_a_marker_that_is_a_link_is_never_followed(self) -> None:
        # Following a link at the marker's path would read or overwrite
        # whatever it names.
        victim = self.root / "victim"
        victim.write_text("victim")
        (self.root / "action").mkdir()
        (self.root / "action" / "tree.writable").symlink_to(victim)
        result = self._run("action", self._manifest(self.b), ["true"], "--writable")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"is not a marker this helper wrote", result.stderr)
        self.assertEqual(victim.read_text(), "victim")

    def test_a_link_leaving_the_tree_is_refused_and_no_tree_is_published(self) -> None:
        # A link out of the tree would hand the command undeclared store or
        # host content through its mount.
        result = self._run(
            "escape",
            self._manifest(self.b, {"node_modules/escape": "../../.."}),
            ["true"],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"leaves the tree", result.stderr)
        self.assertEqual(self._entries(), {self.a[1], self.b[1]})
        self.assertEqual(
            self._objects(), sorted(self._object_of(d) for d in (self.a[1], self.b[1]))
        )

    def test_a_bin_the_archive_left_plain_is_executable_in_the_tree_alone(
        self,
    ) -> None:
        # A tool started by path needs its bin executable whatever mode its
        # archive gave it. The shared
        # expansion keeps the archive's mode, since another tree cloning the
        # same unit may not name that file.
        c = self._archive("c", {"cli.js": b"c\n"})
        manifest = (
            self._manifest(self.b)
            + f"unit|.store/c@1/node_modules/c|{c[1]}|{c[0]}|.\n"
            + "exec|.store/c@1/node_modules/c/cli.js\n"
        )
        result = self._run(
            "executable",
            manifest,
            ["test", "-x", "tree/.store/c@1/node_modules/c/cli.js"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        expansion = self.namespace / "objects" / self._object_of(c[1]) / "cli.js"
        self.assertFalse(os.access(expansion, os.X_OK))

    def test_trees_differing_only_in_their_executables_are_two_trees(self) -> None:
        # A tree published without a file marked executable must not answer a
        # reader that asked for it marked, so the marks are part of the key.
        c = self._archive("c", {"cli.js": b"c\n"})
        plain = (
            self._manifest(self.b) + f"unit|.store/c@1/node_modules/c|{c[1]}|{c[0]}|.\n"
        )
        marked = plain + "exec|.store/c@1/node_modules/c/cli.js\n"
        check = ["test", "-x", "tree/.store/c@1/node_modules/c/cli.js"]
        self.assertNotEqual(self._run("plain", plain, check).returncode, 0)
        result = self._run("marked", marked, check)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self._entries() - {self.a[1], self.b[1], c[1]}), 2)

    def test_an_executable_reached_through_a_link_out_of_its_unit_is_refused(
        self,
    ) -> None:
        # With a strip prefix the clone is part of the expansion, so a link
        # inside it can name a sibling the tree never cloned - here the path
        # where another unit sits - and marking through it would change that
        # unit's file.
        c = self._archive(
            "c",
            {"pkg/cli.js": b"c\n", "other/secret.js": b"s\n"},
            {"pkg/lnk": "../other"},
        )
        d = self._archive("d", {"secret.js": b"d\n"})
        manifest = (
            self._manifest(self.b)
            + f"unit|.store/c@1/node_modules/c|{c[1]}|{c[0]}|pkg\n"
            + f"unit|.store/c@1/node_modules/other|{d[1]}|{d[0]}|.\n"
            + "exec|.store/c@1/node_modules/c/lnk/secret.js\n"
        )
        result = self._run("escape-exec", manifest, ["true"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"leaves its unit", result.stderr)

    def test_a_failed_unit_clone_names_what_it_found_and_the_entry_that_fails(
        self,
    ) -> None:
        # A directory's clone returns one errno for its whole hierarchy, so a
        # rare failure is undiagnosable unless its error says which side was
        # missing and which entry fails alone. An unreadable file in a
        # published expansion fails the unit's clone with EACCES.
        c = self._archive("c", {"index.js": b"c\n", "lib/secret.js": b"s\n"})
        plain = (
            self._manifest(self.b) + f"unit|.store/c@1/node_modules/c|{c[1]}|{c[0]}|.\n"
        )
        self.assertEqual(self._run("first", plain, ["true"]).returncode, 0)
        published = self._objects()
        expansion = self.namespace / "objects" / self._object_of(c[1])
        (expansion / "lib" / "secret.js").chmod(0)

        # Marking a bin makes the second tree a key of its own, so it clones
        # the expansion again.
        marked = plain + "exec|.store/c@1/node_modules/c/index.js\n"
        result = self._run("second", marked, ["true"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"failed with errno 13", result.stderr)
        self.assertIn(
            b"the source is a directory, the destination's parent is a directory"
            b" and the destination is missing",
            result.stderr,
        )
        self.assertIn(
            b"its entry lib/secret.js fails alone with errno 13", result.stderr
        )
        # Neither the failed tree's staging nor the probe's scratch is left.
        self.assertEqual(self._objects(), published)

    def test_an_executable_outside_every_unit_is_refused(self) -> None:
        # Outside a unit the file is one a link reaches, or none the tree
        # holds, so the tree must not change its mode.
        result = self._run(
            "outside",
            self._manifest(self.b) + "exec|node_modules/.bin/a\n",
            ["true"],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"is inside no unit", result.stderr)


if __name__ == "__main__":
    unittest.main()
