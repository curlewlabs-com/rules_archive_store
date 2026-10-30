"""archive_mount.sh must mount exactly the tree an archive declares, and
refuse one it cannot check or a mount it cannot place safely.

These run the real helper through archive_mount_test_support's harness.
"""

from __future__ import annotations

import hashlib
import io
import stat
import subprocess
import tarfile
import unittest

from archive_mount_test_support import (
    HELPER,
    INFINITE,
    NAMESPACE,
    ArchiveMountCase,
    tar_bytes,
)


class ArchiveMountTest(ArchiveMountCase):
    def test_a_tar_expands_with_modes_links_and_empty_directories(self) -> None:
        for name, content in (("tool.parts", tar_bytes()),):
            with self.subTest(name):
                archive, digest = self._archive(name, content)
                result = self._run(
                    "action-" + name,
                    f"mnt/tool|{digest}|{name}|tool-1.0\n",
                    archive,
                    [
                        "/bin/sh",
                        "-c",
                        "mnt/tool/bin/alias && cat mnt/tool/share/data.txt",
                    ],
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, b"run\npayload\n")
                mount = self.root / ("action-" + name) / "mnt/tool"
                self.assertTrue((mount / "empty").is_dir())
                # Read-only, so a tool that writes into its tree fails rather
                # than changing what the next action reads.
                data = mount / "share/data.txt"
                self.assertEqual(stat.S_IMODE(data.stat().st_mode) & 0o222, 0)
                self.assertEqual(stat.S_IMODE(mount.stat().st_mode) & 0o222, 0)

    def test_an_archive_in_another_form_than_the_conversions_is_refused(self) -> None:
        # The helper extracts only the tar declared_archive's conversion
        # wrote, which checked every member and link; a zip or an upstream
        # tar reaches it only through that conversion (archive_to_tar.py).
        for index, (name, content) in enumerate(
            (
                ("tool.zip", b"PK\x05\x06" + bytes(18)),
                ("tool.tar", b"\0" * 1024),
                # A whole tar, not the directory of its parts.
                ("tool.tar.gz", tar_bytes()),
            )
        ):
            with self.subTest(name):
                archive = self.root / name
                archive.write_bytes(content)
                digest = hashlib.sha256(content).hexdigest()
                result = self._run(
                    f"action-{index}", f"tool|{digest}|{name}|.\n", archive, ["true"]
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"is not the tar declared_archive writes", result.stderr)
                self.assertEqual(self._objects(), [])

    def test_a_hostile_tar_or_cd_environment_changes_nothing(self) -> None:
        # TAR_OPTIONS is prepended to GNU tar's own arguments, and CDPATH
        # redirects a relative `cd`. Here CDPATH holds a decoy named like the
        # relative store setting and like the link target the confinement
        # walk enters, so a helper that honored either would expand into the
        # decoy, refuse an in-tree link, or strip the tree's root.
        decoy = self.root / "decoy"
        (decoy / "store").mkdir(parents=True)
        (decoy / "bin").mkdir()
        self.action_env = {
            "TAR_OPTIONS": "--strip-components=1",
            "TAR_READER_OPTIONS": "hdrcharset=BINARY",
            "CDPATH": str(decoy),
        }
        workdir = self.root / "action"
        workdir.mkdir()
        (workdir / "store").symlink_to(self.store)
        self.store_setting = "store"
        archive, digest = self._archive("tool.parts", tar_bytes(link="../bin"))
        result = self._run(
            "action",
            f"tool|{digest}|tool.parts|tool-1.0\n",
            archive,
            ["/bin/sh", "-c", 'cat tool/share/data.txt; printf "%s\\n" "$CDPATH"'],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # The command still gets the caller's CDPATH.
        self.assertEqual(result.stdout, f"payload\n{decoy}\n".encode())
        self.assertEqual(len(self._published()), 1)
        self.assertEqual(sorted(decoy.rglob("*")), [decoy / "bin", decoy / "store"])

    def test_a_member_beneath_one_of_the_tars_links_writes_nothing_outside(
        self,
    ) -> None:
        # The conversion never writes such a tar; this is the helper's own
        # floor beneath it: whatever the system tar does with the child, the
        # sentinel outside the tree is untouched and nothing is published.
        outside = self.root / "outside"
        outside.mkdir()
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for name, kind, link in (
                ("tool-1.0", tarfile.DIRTYPE, ""),
                ("tool-1.0/escape", tarfile.SYMTYPE, str(outside)),
            ):
                info = tarfile.TarInfo(name)
                info.type = kind
                info.linkname = link
                info.mode = 0o755
                tar.addfile(info)
            info = tarfile.TarInfo("tool-1.0/escape/planted")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        archive, digest = self._archive("tool.parts", buffer.getvalue())
        result = self._run(
            "action", f"tool|{digest}|tool.parts|tool-1.0\n", archive, ["true"]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self._published(), [])

    def test_the_command_runs_under_the_callers_umask(self) -> None:
        # The helper widens its umask for the shared store; the command's own
        # outputs must still be made as they would be without it.
        archive, digest = self._archive("tool.parts", tar_bytes())
        result = self._run(
            "action",
            f"tool|{digest}|tool.parts|tool-1.0\n",
            archive,
            ["/bin/sh", "-c", "umask"],
            umask=0o027,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(int(result.stdout, 8), 0o027)

    def test_a_mount_root_holds_the_mounts_and_keeps_the_working_directory(
        self,
    ) -> None:
        # A test's working directory is its runfiles tree, where its archives
        # are; its mounts go in its own temporary directory, and the command
        # must still start where the test runner put it.
        archive, digest = self._archive("tool.parts", tar_bytes())
        workdir = self.root / "runfiles"
        workdir.mkdir()
        (workdir / archive.name).symlink_to(archive)
        (workdir / "manifest").write_text(
            f"nested/tool|{digest}|tool.parts|tool-1.0\n", encoding="ascii"
        )
        mounts = self.root / "test-tmp" / "archive-mounts"
        result = subprocess.run(
            [
                "/bin/sh",
                str(HELPER),
                "--root",
                str(mounts),
                "manifest",
                "--",
                "/bin/sh",
                "-c",
                'pwd -P; cat "$ARCHIVE_MOUNT_ROOT/nested/tool/share/data.txt"',
            ],
            cwd=workdir,
            env={"PATH": "/usr/bin:/bin", "RULES_ARCHIVE_STORE_DIR": str(self.store)},
            capture_output=True,
            check=False,
            timeout=INFINITE,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.decode().splitlines(),
            [str(workdir.resolve()), "payload"],
        )
        self.assertTrue((mounts / "nested/tool").is_symlink())
        self.assertFalse((workdir / "nested").exists())

    def test_a_relative_store_setting_still_links_mounts_correctly(self) -> None:
        # A mount links to the store from a directory of its own, so a store
        # path relative to the action's directory would resolve from the wrong
        # place unless the helper makes it absolute.
        archive, digest = self._archive("tool.parts", tar_bytes())
        self.store_setting = "../store"
        result = self._run(
            "action",
            f"nested/mount/tool|{digest}|tool.parts|tool-1.0\n",
            archive,
            ["cat", "nested/mount/tool/share/data.txt"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"payload\n")

    def test_a_changed_helper_never_reuses_an_earlier_expansion(self) -> None:
        # A helper revision that extracts or checks differently must not read a
        # tree an earlier revision published.
        archive, digest = self._archive("tool.parts", tar_bytes())
        changed = self.root / "archive_mount.sh"
        changed.write_bytes(HELPER.read_bytes() + b"# a later revision\n")
        manifest = f"tool|{digest}|tool.parts|tool-1.0\n"
        for index, helper in enumerate((HELPER, changed)):
            result = self._run(
                f"action-{index}", manifest, archive, ["true"], helper=helper
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b"expanded", result.stderr)
        namespaces = [path for path in self.store.iterdir() if path.is_dir()]
        self.assertEqual(len(namespaces), 2)

    def test_symlinks_leaving_the_tree_are_refused(self) -> None:
        # A link out of the expansion would hand the action undeclared host or
        # store content through its mount, so nothing holding one is published.
        cases = {
            "tar climbing": ("tool.parts", tar_bytes(link="../../../outside")),
            "tar absolute": ("tool.parts", tar_bytes(link="/etc")),
            # Lexically "../up/.." stays in tool-1.0, but `up` is the tree's
            # parent, so the kernel resolves it one level above the tree.
            "tar through a link": (
                "tool.parts",
                tar_bytes(link="../up/..", up=True),
            ),
        }
        for index, (case, (name, content)) in enumerate(cases.items()):
            with self.subTest(case):
                self.store = self.root / f"store-{index}"
                self.store.mkdir()
                archive, digest = self._archive(f"{index}-{name}", content)
                result = self._run(
                    f"action-{index}",
                    f"tool|{digest}|{archive.name}|tool-1.0\n",
                    archive,
                    ["true"],
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"leaves its own tree", result.stderr)
                self.assertEqual(self._published(), [])
                self.assertEqual(self._objects(), [])

    def test_failed_extraction_leaves_no_staging_tree(self) -> None:
        # An archive that hashes correctly but cannot be extracted would
        # otherwise leave a partial tree in the store on every retry.
        archive, digest = self._archive("tool.parts", b"not a tar archive\n")
        result = self._run("action", f"tool|{digest}|tool.parts|.\n", archive, ["true"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._published(), [])
        self.assertEqual(self._objects(), [])

    def test_unsafe_or_overlapping_mounts_are_refused(self) -> None:
        # A mount or strip prefix that climbs out would link outside the
        # action's mount root or the expansion; a mount inside another would
        # depend on manifest order.
        archive, digest = self._archive("tool.parts", tar_bytes())
        cases = {
            "climbing mount": f"../tool|{digest}|tool.parts|tool-1.0\n",
            "absolute strip": f"tool|{digest}|tool.parts|/etc\n",
            "climbing strip": f"tool|{digest}|tool.parts|tool-1.0/..\n",
            "nested mounts": (
                f"tool|{digest}|tool.parts|tool-1.0\n"
                f"tool/bin|{digest}|tool.parts|tool-1.0\n"
            ),
            # One path on a macOS volume, which ignores case.
            "mounts differing in case": (
                f"tool|{digest}|tool.parts|tool-1.0\n"
                f"TOOL|{digest}|tool.parts|tool-1.0\n"
            ),
            "symlink above the mount": f"elsewhere/tool|{digest}|tool.parts|tool-1.0\n",
        }
        for index, (case, manifest) in enumerate(cases.items()):
            with self.subTest(case):
                # A link a prior action left in a shared execution root.
                (self.root / f"action-{index}").mkdir()
                (self.root / f"action-{index}" / "elsewhere").symlink_to(self.root)
                result = self._run(f"action-{index}", manifest, archive, ["true"])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"archive_mount: error:", result.stderr)

    def test_interrupted_expansion_is_never_read(self) -> None:
        # A killed expansion leaves a partial staging directory and no link.
        # The next action must expand afresh rather than read the partial tree.
        archive, digest = self._archive("tool.parts", tar_bytes())
        partial = self.store / NAMESPACE / "objects" / f"{digest}.killed"
        (partial / "tool-1.0/share").mkdir(parents=True)
        (partial / "tool-1.0/share/data.txt").write_bytes(b"pay")
        result = self._run(
            "action",
            f"tool|{digest}|tool.parts|tool-1.0\n",
            archive,
            ["cat", "tool/share/data.txt"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"payload\n")
        (published,) = self._published()
        self.assertNotEqual(published.resolve(), partial.resolve())

    def test_parts_cut_mid_member_mount_as_the_one_tar(self) -> None:
        # A tar too large for one store blob arrives as parts cut wherever a
        # part fills, mostly inside a member's data; joined, they are the tar.
        content = tar_bytes()
        archive, digest = self._archive("tool.parts", content, part_bytes=64)
        self.assertGreater(len(list(archive.iterdir())), 2)
        result = self._run(
            "action",
            f"tool|{digest}|tool.parts|tool-1.0\n",
            archive,
            ["/bin/sh", "-c", "cat tool/share/data.txt; tool/bin/alias"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"payload\nrun\n")
        self.assertEqual([path.name for path in self._published()], [digest])

    def test_a_part_missing_or_out_of_place_publishes_nothing(self) -> None:
        # The digest is the whole tar's, checked over the joined stream tar
        # reads, so a lost or misplaced part never becomes a partial tree.
        content = tar_bytes()
        for index, case in enumerate(("missing", "swapped")):
            with self.subTest(case):
                name = f"{case}.parts"
                archive, digest = self._archive(name, content, part_bytes=64)
                parts = sorted(archive.iterdir())
                if case == "missing":
                    parts[1].unlink()
                else:
                    first, second = parts[0].read_bytes(), parts[1].read_bytes()
                    parts[0].write_bytes(second)
                    parts[1].write_bytes(first)
                result = self._run(
                    f"action-{index}", f"tool|{digest}|{name}|.\n", archive, ["true"]
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"but the manifest declares", result.stderr)
                self.assertEqual(self._published(), [])
                self.assertEqual(self._objects(), [])

    def test_digest_mismatch_publishes_nothing(self) -> None:
        # A manifest digest that does not match the archive would otherwise key
        # the wrong tree for every later action on the instance.
        archive, _ = self._archive("tool.parts", tar_bytes())
        result = self._run(
            "action", f"tool|{'0' * 64}|tool.parts|tool-1.0\n", archive, ["true"]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"but the manifest declares", result.stderr)
        self.assertEqual(self._published(), [])

    def test_digest_file_is_read(self) -> None:
        # A build-produced archive's digest comes from its producer, in
        # shasum's output form.
        archive, digest = self._archive("tool.parts", tar_bytes())
        result = self._run(
            "action",
            "tool|@tool.sha256|tool.parts|.\n",
            archive,
            ["cat", "tool/tool-1.0/share/data.txt"],
            extra={"tool.sha256": f"{digest}  tool.parts\n"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"payload\n")
        self.assertEqual([path.name for path in self._published()], [digest])

    def test_private_store_without_a_configured_store_is_removed(self) -> None:
        # A local developer build sets no store; the expansion must still work
        # and must not outlive the command.
        self.configured = False
        archive, digest = self._archive("tool.parts", tar_bytes())
        result = self._run(
            "action",
            f"tool|{digest}|tool.parts|tool-1.0\n",
            archive,
            ["/bin/sh", "-c", "cat tool/share/data.txt; exit 3"],
        )
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(result.stdout, b"payload\n")
        self.assertEqual(list(self.root.glob("archive-store.*")), [])

    def test_a_private_store_is_removed_under_an_empty_caller_path(self) -> None:
        # Build actions often run with PATH empty. The command gets the
        # caller's PATH back, and the private store's removal runs after it, so
        # that removal must not depend on PATH, or a command that succeeded
        # fails the action.
        self.configured = False
        self.action_env = {"PATH": ""}
        archive, digest = self._archive("tool.tar.gz", tar_bytes())
        result = self._run(
            "action",
            f"tool|{digest}|tool.tar.gz|tool-1.0\n",
            archive,
            ["/bin/cat", "tool/share/data.txt"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"payload\n")
        self.assertEqual(list(self.root.glob("archive-store.*")), [])


if __name__ == "__main__":
    unittest.main()
