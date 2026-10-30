"""archive_to_tar must refuse every archive that could write outside its tree.

The store's helper extracts only tars this conversion wrote, so it is the one
place an archive's member paths and links are judged. Each refusal below is an
archive whose extraction could land a file outside the tree or through one of
its own links.
"""

from __future__ import annotations

import base64
import hashlib
import io
import stat
import tarfile
import unicodedata
import unittest

import archive_drop
import archive_to_tar
from archive_to_tar_test_support import (
    ConversionTestCase,
    Member,
    TarMember,
    convert,
    joined,
    tar_bytes,
    written,
    zip_bytes,
)

TREE: list[Member] = [
    ("tool-1.0/", stat.S_IFDIR | 0o755, b""),
    ("tool-1.0/bin/run", stat.S_IFREG | 0o755, b"#!/bin/sh\necho run\n"),
    ("tool-1.0/bin/alias", stat.S_IFLNK | 0o777, b"run"),
    ("tool-1.0/empty/", stat.S_IFDIR | 0o755, b""),
    ("tool-1.0/share/data.txt", stat.S_IFREG | 0o644, b"payload\n"),
]


TAR_TREE: list[TarMember] = [
    ("./tool-1.0", tarfile.DIRTYPE, 0o755, b"", ""),
    ("./tool-1.0/bin", tarfile.DIRTYPE, 0o755, b"", ""),
    ("./tool-1.0/bin/run", tarfile.REGTYPE, 0o4755, b"#!/bin/sh\necho run\n", ""),
    ("./tool-1.0/bin/alias", tarfile.SYMTYPE, 0o777, b"", "run"),
    ("./tool-1.0/bin/again", tarfile.LNKTYPE, 0o755, b"", "./tool-1.0/bin/run"),
    # A tar may name a directory again; it changes nothing.
    ("./tool-1.0/bin", tarfile.DIRTYPE, 0o755, b"", ""),
    ("./tool-1.0/empty", tarfile.DIRTYPE, 0o755, b"", ""),
]


def _listed(names: list[str]) -> str:
    """The digest of a drop list naming `names`, as declared_archive declares it."""
    return hashlib.sha256("".join(f"{name}\n" for name in names).encode()).hexdigest()


# A well-formed digest for drops a test refuses before the digest is compared.
_UNUSED_DIGEST = "0" * 64


class ArchiveToTarTest(ConversionTestCase):
    def test_the_tree_keeps_its_modes_links_and_empty_directories(self) -> None:
        output = self.convert_zip(TREE)
        with tarfile.open(output) as tar:
            entries = {entry.name: entry for entry in tar.getmembers()}
            self.assertEqual(entries["tool-1.0/bin/run"].mode, 0o755)
            self.assertEqual(entries["tool-1.0/share/data.txt"].mode, 0o644)
            self.assertTrue(entries["tool-1.0/bin/alias"].issym())
            self.assertEqual(entries["tool-1.0/bin/alias"].linkname, "run")
            self.assertTrue(entries["tool-1.0/empty"].isdir())
            data = tar.extractfile("tool-1.0/share/data.txt")
            assert data is not None
            self.assertEqual(data.read(), b"payload\n")

    def test_the_same_zip_gives_the_same_tar_bytes(self) -> None:
        # The tar's digest keys the store and enters consumers' action keys,
        # so it may depend on the zip's bytes alone, not on when or where the
        # conversion ran.
        first = self.convert_zip(TREE, "first").read_bytes()
        second = self.convert_zip(TREE, "second").read_bytes()
        self.assertEqual(first, second)

    def test_zips_that_would_write_outside_the_tree_are_refused(self) -> None:
        link = stat.S_IFLNK | 0o777
        file = stat.S_IFREG | 0o644
        cases: dict[str, list[Member]] = {
            "member beneath a link": [
                ("tool-1.0/escape", link, b"/tmp"),
                ("tool-1.0/escape/planted", file, b"x"),
            ],
            # The link stays in the tree, so only the beneath-a-link check
            # can refuse the member an extractor would write through it.
            "member beneath an in-tree link": [
                ("tool-1.0/real/", stat.S_IFDIR | 0o755, b""),
                ("tool-1.0/alias", link, b"real"),
                ("tool-1.0/alias/planted", file, b"x"),
            ],
            # The same member named through `./`, which a comparison of raw
            # names would miss.
            "member beneath a link through ./": [
                ("tool-1.0/escape", link, b"../.."),
                ("tool-1.0/./escape/planted", file, b"x"),
            ],
            "absolute link": [("tool-1.0/escape", link, b"/etc")],
            "climbing link": [("tool-1.0/escape", link, b"../../outside")],
            "climbing member": [("tool-1.0/../../outside", file, b"x")],
            "absolute member": [("/etc/planted", file, b"x")],
            "member named twice": [
                ("tool-1.0/data.txt", file, b"first"),
                ("tool-1.0/./data.txt", file, b"second"),
            ],
            # A tar may repeat a directory; a zip never needed to, and
            # still may not.
            "directory named twice": [
                ("tool-1.0/", stat.S_IFDIR | 0o755, b""),
                ("tool-1.0/./", stat.S_IFDIR | 0o755, b""),
            ],
        }
        for index, (case, members) in enumerate(cases.items()):
            with self.subTest(case), self.assertRaises(archive_to_tar.ArchiveError):
                self.convert_zip(members, f"case-{index}")

    def test_a_tar_keeps_its_tree_with_links_hard_links_and_no_set_id_bits(
        self,
    ) -> None:
        output = self.convert_tar(TAR_TREE, "tar")
        with tarfile.open(output) as tar:
            entries = {entry.name: entry for entry in tar.getmembers()}
            # The repeated directory is written once.
            self.assertEqual(
                [entry.name for entry in tar.getmembers()].count("tool-1.0/bin"), 1
            )
            self.assertEqual(entries["tool-1.0/bin/run"].mode, 0o755)
            self.assertEqual(entries["tool-1.0/bin/alias"].linkname, "run")
            self.assertTrue(entries["tool-1.0/bin/again"].islnk())
            self.assertEqual(entries["tool-1.0/bin/again"].linkname, "tool-1.0/bin/run")
            self.assertTrue(entries["tool-1.0/empty"].isdir())
            self.assertTrue(
                all(entry.mtime == 0 and entry.uid == 0 for entry in entries.values())
            )

    def test_every_tar_compression_converts_to_the_same_tar(self) -> None:
        # The tar the store keys by depends on the tree, not on how upstream
        # compressed it.
        outputs = {
            mode: self.convert_tar(TAR_TREE, f"tree-{index}", mode).read_bytes()
            for index, mode in enumerate(("w", "w:gz", "w:xz", "w:bz2", "w:zst"))
        }
        self.assertEqual(len(set(outputs.values())), 1, list(outputs))

    def test_tars_that_would_write_outside_the_tree_are_refused(self) -> None:
        directory, regular, link, hard = (
            tarfile.DIRTYPE,
            tarfile.REGTYPE,
            tarfile.SYMTYPE,
            tarfile.LNKTYPE,
        )
        cases: dict[str, list[TarMember]] = {
            # A tar extractor that follows the link would write the child
            # wherever the link points.
            "member beneath a link": [
                ("tool-1.0/escape", link, 0o777, b"", "../.."),
                ("tool-1.0/escape/planted", regular, 0o644, b"x", ""),
            ],
            "member beneath an in-tree link": [
                ("tool-1.0/real", directory, 0o755, b"", ""),
                ("tool-1.0/alias", link, 0o777, b"", "real"),
                ("tool-1.0/alias/planted", regular, 0o644, b"x", ""),
            ],
            "absolute link": [("tool-1.0/escape", link, 0o777, b"", "/etc")],
            "climbing link": [("tool-1.0/escape", link, 0o777, b"", "../../outside")],
            "climbing member": [("tool-1.0/../../outside", regular, 0o644, b"x", "")],
            "absolute member": [("/etc/planted", regular, 0o644, b"x", "")],
            "file named twice": [
                ("tool-1.0/data.txt", regular, 0o644, b"first", ""),
                ("tool-1.0/./data.txt", regular, 0o644, b"second", ""),
            ],
            "directory then file of one name": [
                ("tool-1.0/data", directory, 0o755, b"", ""),
                ("tool-1.0/data", regular, 0o644, b"x", ""),
            ],
            "hard link out of the tree": [
                ("tool-1.0/passwd", hard, 0o644, b"", "../../etc/passwd")
            ],
            "hard link to a later member": [
                ("tool-1.0/early", hard, 0o644, b"", "tool-1.0/late"),
                ("tool-1.0/late", regular, 0o644, b"x", ""),
            ],
            "hard link to a link": [
                ("tool-1.0/alias", link, 0o777, b"", "data"),
                ("tool-1.0/data", regular, 0o644, b"x", ""),
                ("tool-1.0/again", hard, 0o644, b"", "tool-1.0/alias"),
            ],
            "device": [("tool-1.0/null", tarfile.CHRTYPE, 0o666, b"", "")],
            "fifo": [("tool-1.0/pipe", tarfile.FIFOTYPE, 0o644, b"", "")],
        }
        for index, (case, members) in enumerate(cases.items()):
            with self.subTest(case), self.assertRaises(archive_to_tar.ArchiveError):
                self.convert_tar(members, f"bad-{index}")

    def test_a_name_whose_written_form_differs_from_its_checked_form_is_refused(
        self,
    ) -> None:
        # A tar field ends at its first NUL, so `escape\0/extra` would be
        # checked as a path beneath `escape` and written as `escape` itself,
        # a climbing link the check never saw. The root, `.`, may only be a
        # directory, since a file there would replace the tree.
        directory, regular, link = tarfile.DIRTYPE, tarfile.REGTYPE, tarfile.SYMTYPE
        cases: dict[str, list[TarMember]] = {
            "file at the root": [("tool-1.0/..", regular, 0o644, b"x", "")],
            "link at the root": [("./", link, 0o777, b"", "..")],
            "directory repeated with another mode": [
                ("tool-1.0", directory, 0o755, b"", ""),
                ("tool-1.0", directory, 0o700, b"", ""),
            ],
        }
        for index, (case, members) in enumerate(cases.items()):
            with self.subTest(case), self.assertRaises(archive_to_tar.ArchiveError):
                self.convert_tar(members, f"identity-{index}", mode="w:gz")

    def test_a_link_name_holding_a_nul_in_its_pax_record_is_refused(self) -> None:
        # Checked as `escape\0/extra`, the link resolves `../..` to the tree
        # itself; written, its name ends at the NUL, so the tar would hold
        # `escape -> ../..` climbing out, with `escape/planted` beneath it.
        source = self.root / "nul-name.tar.gz"
        with tarfile.open(source, "w:gz", format=tarfile.PAX_FORMAT) as tar:
            info = tarfile.TarInfo("tool-1.0/placeholder")
            info.type = tarfile.SYMTYPE
            info.linkname = "../.."
            info.pax_headers = {"path": "tool-1.0/escape\0/extra"}
            tar.addfile(info)
            planted = tarfile.TarInfo("tool-1.0/escape/planted")
            planted.size = 1
            tar.addfile(planted, io.BytesIO(b"x"))
        with tarfile.open(source) as tar:
            self.assertIn("\0", tar.getmembers()[0].name)
        with self.assertRaises(archive_to_tar.ArchiveError):
            convert(source, self.root / "nul-name.out.tar.gz")

    def test_a_link_target_holding_a_nul_in_its_pax_record_is_refused(self) -> None:
        # A PAX record carries a target past a NUL that the fixed field would
        # end at, so the target checked and the target written could differ.
        source = self.root / "nul-target.tar.gz"
        with tarfile.open(source, "w:gz", format=tarfile.PAX_FORMAT) as tar:
            info = tarfile.TarInfo("tool-1.0/alias")
            info.type = tarfile.SYMTYPE
            info.pax_headers = {"linkpath": "run\0/../../outside"}
            tar.addfile(info)
        with tarfile.open(source) as tar:
            self.assertIn("\0", tar.getmember("tool-1.0/alias").linkname)
        with self.assertRaises(archive_to_tar.ArchiveError):
            convert(source, self.root / "nul-target.out.tar.gz")

    def test_the_root_directory_entry_is_accepted_and_left_out(self) -> None:
        output = self.convert_tar(
            [("./", tarfile.DIRTYPE, 0o700, b"", ""), *TAR_TREE], "rooted"
        )
        with tarfile.open(output) as tar:
            self.assertNotIn(".", tar.getnames())
            self.assertIn("tool-1.0/bin/run", tar.getnames())

    def test_a_deep_path_is_judged_by_its_components(self) -> None:
        # The links are kept as a tree of components, so a deep path is
        # walked once; it must still find a link many levels up, and must
        # not mistake a name that merely starts with a link's for one beneath it.
        deep = "/".join(["tool-1.0", "alias", *(["d"] * 1500), "planted"])
        regular, link, directory = tarfile.REGTYPE, tarfile.SYMTYPE, tarfile.DIRTYPE
        with self.assertRaises(archive_to_tar.ArchiveError):
            self.convert_tar(
                [
                    ("tool-1.0/real", directory, 0o755, b"", ""),
                    ("tool-1.0/alias", link, 0o777, b"", "real"),
                    (deep, regular, 0o644, b"x", ""),
                ],
                "deep-beneath",
            )
        self.convert_tar(
            [
                ("tool-1.0/real", directory, 0o755, b"", ""),
                ("tool-1.0/alias", link, 0o777, b"", "real"),
                (deep.replace("/alias/", "/aliased/"), regular, 0o644, b"x", ""),
                ("tool-1.0/al", regular, 0o644, b"x", ""),
            ],
            "deep-beside",
        )

    def test_names_folded_cannot_judge_are_refused(self) -> None:
        # A control character no tree here holds, and a name that is not
        # UTF-8, which tarfile reads as a lone surrogate and APFS cannot store.
        for index, name in enumerate(("tool-1.0/a\x01b", "tool-1.0/a\udcffb")):
            with (
                self.subTest(name=repr(name)),
                self.assertRaises(archive_to_tar.ArchiveError),
            ):
                archive_to_tar._normalized(name)
        source = self.root / "latin1.tar.gz"
        with tarfile.open(
            source, "w:gz", format=tarfile.USTAR_FORMAT, encoding="latin-1"
        ) as tar:
            info = tarfile.TarInfo("tool-1.0/caf\u00e9")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        with self.assertRaises(archive_to_tar.ArchiveError):
            convert(source, self.root / "latin1.out.tar.gz")

    def test_a_name_holding_a_character_unknown_to_the_tables_is_refused(self) -> None:
        # Its folding is unknown here, and a Mac volume newer than these
        # tables may merge it with a character they know.
        unknown = next(
            chr(code)
            for code in range(0xA700, 0xA800)
            if unicodedata.category(chr(code)) == "Cn"
        )
        with self.assertRaises(archive_to_tar.ArchiveError):
            self.convert_tar(
                [(f"tool-1.0/x{unknown}", tarfile.REGTYPE, 0o644, b"x", "")], "unknown"
            )
        with self.assertRaises(archive_to_tar.ArchiveError):
            self.convert_zip(
                [(f"tool-1.0/x{unknown}", stat.S_IFREG | 0o644, b"x")], "unknown-zip"
            )

    def _colliding(self) -> list[TarMember]:
        """A tree whose one directory holds names a case-insensitive volume
        merges, as the NDK's netfilter headers are."""
        regular, directory = tarfile.REGTYPE, tarfile.DIRTYPE
        return [
            ("tool-1.0", directory, 0o755, b"", ""),
            ("tool-1.0/keep.txt", regular, 0o644, b"keep", ""),
            ("tool-1.0/include", directory, 0o755, b"", ""),
            ("tool-1.0/include/netfilter", directory, 0o755, b"", ""),
            ("tool-1.0/include/netfilter/xt_MARK.h", regular, 0o644, b"upper", ""),
            ("tool-1.0/include/netfilter/xt_mark.h", regular, 0o644, b"lower", ""),
        ]

    def test_a_dropped_directory_and_everything_beneath_it_is_left_out(self) -> None:
        # The NDK zip converts only with its case-colliding headers dropped;
        # a drop takes the directory a pattern names and all beneath it, and
        # the rest of the tree converts as before.
        drop = archive_drop.Drop(
            ["tool-1.0/include/net*"],
            _listed(
                [
                    "tool-1.0/include/netfilter",
                    "tool-1.0/include/netfilter/xt_MARK.h",
                    "tool-1.0/include/netfilter/xt_mark.h",
                ]
            ),
        )
        source = self.root / "colliding.tar.gz"
        source.write_bytes(tar_bytes(self._colliding()))
        output = self.root / "colliding.out.tar.gz"
        convert(source, output, drop)
        with tarfile.open(output) as tar:
            names = tar.getnames()
        self.assertEqual(names, ["tool-1.0", "tool-1.0/keep.txt", "tool-1.0/include"])
        self.assertEqual(
            drop.dropped,
            [
                "tool-1.0/include/netfilter",
                "tool-1.0/include/netfilter/xt_MARK.h",
                "tool-1.0/include/netfilter/xt_mark.h",
            ],
        )
        zip_source = self.root / "colliding.zip"
        zip_source.write_bytes(
            zip_bytes(
                [
                    ("tool-1.0/keep.txt", stat.S_IFREG | 0o644, b"keep"),
                    (
                        "tool-1.0/include/netfilter/xt_MARK.h",
                        stat.S_IFREG | 0o644,
                        b"u",
                    ),
                    (
                        "tool-1.0/include/netfilter/xt_mark.h",
                        stat.S_IFREG | 0o644,
                        b"l",
                    ),
                ]
            )
        )
        convert(
            zip_source,
            self.root / "colliding-zip.tar.gz",
            archive_drop.Drop(
                ["tool-1.0/include"],
                _listed(
                    [
                        "tool-1.0/include/netfilter/xt_MARK.h",
                        "tool-1.0/include/netfilter/xt_mark.h",
                    ]
                ),
            ),
        )

    def test_a_collision_the_drop_does_not_reach_is_still_refused(self) -> None:
        members = [
            *self._colliding(),
            ("tool-1.0/KEEP.txt", tarfile.REGTYPE, 0o644, b"k", ""),
        ]
        source = self.root / "still.tar.gz"
        source.write_bytes(tar_bytes(members))
        with self.assertRaises(archive_to_tar.ArchiveError):
            convert(
                source,
                self.root / "still.out",
                archive_drop.Drop(["tool-1.0/include"], _UNUSED_DIGEST),
            )

    def test_a_drop_that_leaves_out_anything_but_its_declared_set_fails(self) -> None:
        # A pin bump can add a file beneath a dropped directory while the
        # pattern still matches; the digest of the dropped names must change
        # with it, or unreviewed content would be dropped.
        declared = _listed(
            [
                "tool-1.0/include/netfilter",
                "tool-1.0/include/netfilter/xt_MARK.h",
                "tool-1.0/include/netfilter/xt_mark.h",
            ]
        )
        grown = [
            *self._colliding(),
            ("tool-1.0/include/netfilter/xt_new.h", tarfile.REGTYPE, 0o644, b"new", ""),
        ]
        source = self.root / "grown.tar.gz"
        source.write_bytes(tar_bytes(grown))
        output = self.root / "grown.out.tar.gz"
        with self.assertRaisesRegex(archive_drop.DropError, "xt_new.h"):
            convert(
                source,
                output,
                archive_drop.Drop(["tool-1.0/include/netfilter"], declared),
            )
        self.assertFalse(written(output))

    def test_a_drop_pattern_matching_nothing_fails(self) -> None:
        # A pin bump that moved the dropped directory must not leave a
        # pattern silently dropping nothing.
        source = self.root / "unmatched.tar.gz"
        source.write_bytes(tar_bytes(self._colliding()))
        with self.assertRaisesRegex(archive_drop.DropError, "tool-1.0/gone"):
            convert(
                source,
                self.root / "unmatched.out",
                archive_drop.Drop(
                    ["tool-1.0/include", "tool-1.0/gone"], _UNUSED_DIGEST
                ),
            )

    def test_a_drop_pattern_matches_whole_components(self) -> None:
        # `*` stands for one component, so a pattern cannot reach deeper or
        # sideways than it names.
        drop = archive_drop.Drop(["tool-1.0/*/netfilter"], _UNUSED_DIGEST)
        self.assertTrue(drop.drops("tool-1.0/include/netfilter/xt_mark.h"))
        self.assertFalse(drop.drops("tool-1.0/a/b/netfilter"))
        self.assertFalse(drop.drops("tool-1.0/include/netfilterx"))
        for pattern in ("", "/tool", "tool-1.0/../x", "tool-1.0//x", "./tool-1.0"):
            with (
                self.subTest(pattern=pattern),
                self.assertRaises(archive_drop.DropError),
            ):
                archive_drop.Drop([pattern], _UNUSED_DIGEST)

    def test_the_cli_lists_what_it_dropped(self) -> None:
        source = self.root / "cli.tar.gz"
        source.write_bytes(tar_bytes(self._colliding()))
        dropped = self.root / "cli.dropped.txt"
        status = archive_to_tar.main(
            [
                "--archive",
                str(source),
                "--digest",
                hashlib.sha256(source.read_bytes()).hexdigest(),
                "--tar-parts",
                str(self.root / "cli.parts"),
                "--part-bytes",
                str(1 << 20),
                "--tar-digest",
                str(self.root / "cli.sha256"),
                "--drop",
                "tool-1.0/include",
                "--dropped",
                str(dropped),
                "--dropped-digest",
                _listed(
                    [
                        "tool-1.0/include",
                        "tool-1.0/include/netfilter",
                        "tool-1.0/include/netfilter/xt_MARK.h",
                        "tool-1.0/include/netfilter/xt_mark.h",
                    ]
                ),
            ]
        )
        self.assertEqual(status, 0)
        self.assertEqual(
            dropped.read_text(encoding="utf-8").splitlines(),
            [
                "tool-1.0/include",
                "tool-1.0/include/netfilter",
                "tool-1.0/include/netfilter/xt_MARK.h",
                "tool-1.0/include/netfilter/xt_mark.h",
            ],
        )

    def test_paths_a_case_insensitive_volume_would_merge_are_refused(self) -> None:
        # A macOS volume ignores case and Unicode normalization, so these
        # pairs extract to one path there: the members would overwrite each
        # other, and a member beneath `alias/` would land beneath the link
        # `Alias`.
        regular, link, directory = tarfile.REGTYPE, tarfile.SYMTYPE, tarfile.DIRTYPE
        composed, decomposed = "caf\u00e9", "cafe\u0301"
        tar_cases: dict[str, list[TarMember]] = {
            "case only": [
                ("tool-1.0/Data", regular, 0o644, b"a", ""),
                ("tool-1.0/data", regular, 0o644, b"b", ""),
            ],
            "normalization only": [
                (f"tool-1.0/{composed}", regular, 0o644, b"a", ""),
                (f"tool-1.0/{decomposed}", regular, 0o644, b"b", ""),
            ],
            "member beneath a link of another case": [
                ("tool-1.0/real", directory, 0o755, b"", ""),
                ("tool-1.0/Alias", link, 0o777, b"", "real"),
                ("tool-1.0/alias/planted", regular, 0o644, b"x", ""),
            ],
        }
        for index, (case, members) in enumerate(tar_cases.items()):
            with (
                self.subTest(f"tar {case}"),
                self.assertRaises(archive_to_tar.ArchiveError),
            ):
                self.convert_tar(members, f"fold-{index}")
        file, zip_link = stat.S_IFREG | 0o644, stat.S_IFLNK | 0o777
        zip_cases: dict[str, list[Member]] = {
            "case only": [("tool-1.0/Data", file, b"a"), ("tool-1.0/data", file, b"b")],
            "normalization only": [
                (f"tool-1.0/{composed}", file, b"a"),
                (f"tool-1.0/{decomposed}", file, b"b"),
            ],
            "member beneath a link of another case": [
                ("tool-1.0/real/", stat.S_IFDIR | 0o755, b""),
                ("tool-1.0/Alias", zip_link, b"real"),
                ("tool-1.0/alias/planted", file, b"x"),
            ],
        }
        for index, (case, zip_members) in enumerate(zip_cases.items()):
            with (
                self.subTest(f"zip {case}"),
                self.assertRaises(archive_to_tar.ArchiveError),
            ):
                self.convert_zip(zip_members, f"fold-zip-{index}")
        # Names that differ in more than case still convert.
        self.convert_tar(
            [
                ("tool-1.0/Data", regular, 0o644, b"a", ""),
                ("tool-1.0/data2", regular, 0o644, b"b", ""),
            ],
            "fold-distinct",
        )

    def test_an_archive_of_another_kind_is_refused(self) -> None:
        source = self.root / "tool.rar"
        source.write_bytes(b"Rar!")
        with self.assertRaises(archive_to_tar.ArchiveError):
            convert(source, self.root / "tool.tar.gz")

    def test_a_zip_not_matching_its_digest_is_refused(self) -> None:
        # The tar digest it writes is what binds consumers to these bytes, so
        # a zip that is not the declared one must produce nothing.
        source = self.root / "tool.zip"
        source.write_bytes(zip_bytes(TREE))
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        (self.root / "tool.zip.sha256").write_text(f"{digest}  tool.zip\n")
        for declared, ok in (
            (f"@{self.root / 'tool.zip.sha256'}", True),
            ("0" * 64, False),
        ):
            with self.subTest(ok=ok):
                tar_digest = self.root / f"out-{ok}.sha256"
                tar = self.root / f"out-{ok}.parts"
                status = archive_to_tar.main(
                    [
                        "--archive",
                        str(source),
                        "--digest",
                        declared,
                        "--tar-parts",
                        str(tar),
                        "--part-bytes",
                        str(1 << 20),
                        "--tar-digest",
                        str(tar_digest),
                    ]
                )
                self.assertEqual(status == 0, ok)
                self.assertEqual(tar_digest.exists(), ok)
                if ok:
                    self.assertEqual(
                        tar_digest.read_text(encoding="ascii"),
                        hashlib.sha256(joined(tar)).hexdigest() + "\n",
                    )

    def test_stripped_components_root_the_tree_whatever_the_top_is_named(
        self,
    ) -> None:
        # npm strips a tarball's top directory by position, not by name, and
        # some locked tarballs name it neither `package` nor alike, such as
        # `node v22.20`. A hard link names its target by the same stripped path.
        regular, directory = tarfile.REGTYPE, tarfile.DIRTYPE
        source = self.root / "stripped.tgz"
        source.write_bytes(
            tar_bytes(
                [
                    ("node v22.20", directory, 0o755, b"", ""),
                    ("node v22.20/package.json", regular, 0o644, b"{}", ""),
                    ("node v22.20/lib/a.js", regular, 0o644, b"a", ""),
                    (
                        "node v22.20/lib/b.js",
                        tarfile.LNKTYPE,
                        0o644,
                        b"",
                        "node v22.20/lib/a.js",
                    ),
                ]
            )
        )
        output = self.root / "stripped.out.tar.gz"
        convert(source, output, strip_components=1)
        with tarfile.open(output) as tar:
            entries = {entry.name: entry for entry in tar.getmembers()}
        self.assertEqual(sorted(entries), ["lib/a.js", "lib/b.js", "package.json"])
        self.assertEqual(entries["lib/b.js"].linkname, "lib/a.js")

    def test_a_file_above_the_stripped_root_is_refused(self) -> None:
        # Stripping would leave it no name; the tree's root is a directory.
        source = self.root / "above.tgz"
        source.write_bytes(tar_bytes([("README", tarfile.REGTYPE, 0o644, b"x", "")]))
        with self.assertRaises(archive_to_tar.ArchiveError):
            convert(source, self.root / "above.out", strip_components=1)
        self.assertFalse(written(self.root / "above.out"))

    def test_a_file_named_again_is_accepted_only_unchanged(self) -> None:
        # npm's tarballs name some files twice, once through `./`: written
        # once, they change nothing. A repeat with other bytes would make the
        # tree depend on which one an extractor keeps, so it is refused.
        regular = tarfile.REGTYPE
        same = self.convert_tar(
            [
                ("package/./dist/index.js", regular, 0o644, b"same", ""),
                ("package/dist/index.js", regular, 0o644, b"same", ""),
            ],
            "repeat-same",
        )
        with tarfile.open(same) as tar:
            self.assertEqual(
                [entry.name for entry in tar.getmembers()], ["package/dist/index.js"]
            )
        for index, (mode, content) in enumerate(((0o644, b"diff"), (0o755, b"same"))):
            with (
                self.subTest(mode=oct(mode), content=content),
                self.assertRaises(archive_to_tar.ArchiveError),
            ):
                self.convert_tar(
                    [
                        ("package/./dist/index.js", regular, 0o644, b"same", ""),
                        ("package/dist/index.js", regular, mode, content, ""),
                    ],
                    f"repeat-changed-{index}",
                )

    def test_a_tarball_is_checked_against_its_npm_integrity(self) -> None:
        # An npm lock pins a tarball by its SRI SHA-512, which the conversion
        # checks itself rather than trusting a digest file beside it.
        source = self.root / "package.tgz"
        source.write_bytes(
            tar_bytes([("package/package.json", tarfile.REGTYPE, 0o644, b"{}", "")])
        )
        integrity = (
            "sha512-"
            + base64.b64encode(hashlib.sha512(source.read_bytes()).digest()).decode()
        )
        other = "sha512-" + base64.b64encode(hashlib.sha512(b"").digest()).decode()
        for declared, ok in ((integrity, True), (other, False), ("sha512-AAAA", False)):
            with self.subTest(declared=declared[:16], ok=ok):
                tar = self.root / f"npm-{ok}-{len(declared)}.parts"
                status = archive_to_tar.main(
                    [
                        "--archive",
                        str(source),
                        "--digest",
                        declared,
                        "--tar-parts",
                        str(tar),
                        "--part-bytes",
                        str(1 << 20),
                        "--tar-digest",
                        str(self.root / f"npm-{ok}-{len(declared)}.sha256"),
                        "--strip-components",
                        "1",
                    ]
                )
                self.assertEqual(status == 0, ok)
                if ok:
                    with tarfile.open(fileobj=io.BytesIO(joined(tar))) as written_tar:
                        self.assertEqual(written_tar.getnames(), ["package.json"])


if __name__ == "__main__":
    unittest.main()
