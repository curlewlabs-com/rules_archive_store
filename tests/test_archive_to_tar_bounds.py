"""archive_to_tar must hold a worker's memory to fixed bounds on any archive.

Each test is an archive whose conversion, by streaming a member, reading an
extended header or a zip's directory, or holding names, would take memory in
proportion to something a malformed archive controls; each must be refused, or
converted within a bound that does not grow with it. These ceilings guard a
worker against a malformed archive, not an adversary.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import stat
import tarfile
import tracemalloc
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import archive_to_tar
from archive_to_tar_test_support import (
    ConversionTestCase,
    convert,
    written,
)


class ArchiveToTarBoundsTest(ConversionTestCase):
    def test_a_large_member_streams_rather_than_being_held(self) -> None:
        # An SDK member can expand far past a worker's memory; the conversion
        # must hold a bounded window of it, not all of it.
        size = 64 << 20
        source = self.root / "big.zip"
        with (
            zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive,
            archive.open("tool-1.0/big.bin", "w") as member,
        ):
            chunk = b"\0" * (1 << 20)
            for _ in range(size // len(chunk)):
                member.write(chunk)
        with zipfile.ZipFile(source) as archive:
            stored = archive.getinfo("tool-1.0/big.bin")
        # Deflated, so the test reads through decompression, as an SDK does.
        self.assertEqual(stored.compress_type, zipfile.ZIP_DEFLATED)
        self.assertLess(stored.compress_size, size // 100)
        output = self.root / "big.tar.gz"
        tracemalloc.start()
        try:
            convert(source, output)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, size // 8)
        with tarfile.open(output) as tar:
            self.assertEqual(tar.getmember("tool-1.0/big.bin").size, size)

    def test_a_large_tar_member_streams_under_every_compression(self) -> None:
        # Each compressed tar is read through its module's own file class,
        # which bounds each read's output; tarfile's own stream reader would
        # expand a whole buffered chunk of a high-ratio tar at once.
        size = 64 << 20
        zeros = self.root / "zeros"
        with zeros.open("wb") as stream:
            stream.truncate(size)
        for mode, suffix in (
            ("w:gz", ".tar.gz"),
            ("w:xz", ".tar.xz"),
            ("w:bz2", ".tar.bz2"),
            ("w:zst", ".tar.zst"),
        ):
            with self.subTest(suffix):
                source = self.root / f"big{suffix}"
                with tarfile.open(source, mode) as tar:  # type: ignore[call-overload]
                    info = tarfile.TarInfo("tool-1.0/big.bin")
                    info.size = size
                    with zeros.open("rb") as stream:
                        tar.addfile(info, stream)
                output = self.root / f"big{suffix}.out.tar.gz"
                tracemalloc.start()
                try:
                    convert(source, output)
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                self.assertLess(peak, size // 4)

    def test_a_link_payload_over_the_bound_is_refused_unread(self) -> None:
        # A link's target is read whole, so its size is checked first.
        link = stat.S_IFLNK | 0o777
        target = b"a" * (archive_to_tar.LINK_TARGET_MAX + 1)
        with self.assertRaises(archive_to_tar.ArchiveError):
            self.convert_zip([("tool-1.0/alias", link, target)], "long-link")
        at_bound = b"a" * archive_to_tar.LINK_TARGET_MAX
        output = self.convert_zip([("tool-1.0/alias", link, at_bound)], "bound-link")
        with tarfile.open(output) as tar:
            self.assertEqual(
                tar.getmember("tool-1.0/alias").linkname, at_bound.decode()
            )

    def test_a_link_understating_its_size_is_read_only_to_the_bound(self) -> None:
        # The zip's central directory says the link's target is a few bytes,
        # while its deflated data expands to tens of MiB; reading it whole
        # would decompress all of that before the size was ever checked.
        expanded = 32 << 20
        source = self.root / "lying.zip"
        with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
            info = zipfile.ZipInfo("tool-1.0/alias")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, b"\0" * expanded)
        data = bytearray(source.read_bytes())
        central = data.rindex(b"PK\x01\x02")
        # The central directory entry's uncompressed size, which zipfile reads.
        data[central + 24 : central + 28] = (16).to_bytes(4, "little")
        source.write_bytes(bytes(data))
        tracemalloc.start()
        try:
            with self.assertRaises((archive_to_tar.ArchiveError, zipfile.BadZipFile)):
                convert(source, self.root / "lying.tar.gz")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, expanded // 8)

    def test_members_compressed_by_unbounded_methods_are_refused(self) -> None:
        # zipfile reads bzip2, LZMA and Zstandard members with no output
        # limit, so a high-ratio one could expand past memory in one read;
        # they are refused before anything is decompressed, links included.
        file = stat.S_IFREG | 0o644
        link = stat.S_IFLNK | 0o777
        for method in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA, zipfile.ZIP_ZSTANDARD):
            for kind, name in ((file, "data.bin"), (link, "alias")):
                with self.subTest(method=method, kind=name):
                    source = self.root / f"method-{method}-{name}.zip"
                    with zipfile.ZipFile(source, "w") as archive:
                        info = zipfile.ZipInfo(f"tool-1.0/{name}")
                        info.external_attr = kind << 16
                        info.compress_type = method
                        archive.writestr(info, b"\0" * (1 << 20))
                    output = self.root / f"method-{method}-{name}.tar.gz"
                    with self.assertRaises(archive_to_tar.ArchiveError):
                        convert(source, output)
                    self.assertFalse(written(output))

    def test_an_oversized_extended_header_is_refused_before_it_is_read(self) -> None:
        # tarfile reads an extended header whole before yielding its member,
        # so one past the bound must be refused from its size alone; zeros
        # keep it a few KiB on disk.
        big = archive_to_tar.EXTENDED_HEADER_MAX + 1
        cases = {
            "PAX record": (
                tarfile.PAX_FORMAT,
                "tool-1.0/data",
                {"comment": "\0" * big},
            ),
            "GNU long name": (tarfile.GNU_FORMAT, "tool-1.0/" + "a" * big, {}),
        }
        for index, (case, (format, name, headers)) in enumerate(cases.items()):
            with self.subTest(case):
                source = self.root / f"extended-{index}.tar.gz"
                with tarfile.open(source, "w:gz", format=format) as tar:
                    info = tarfile.TarInfo(name)
                    info.pax_headers = headers
                    tar.addfile(info, io.BytesIO(b""))
                self.assertLess(source.stat().st_size, big // 16)
                tracemalloc.start()
                try:
                    with self.assertRaises(archive_to_tar.ArchiveError):
                        convert(source, self.root / f"extended-{index}.out")
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                self.assertLess(peak, big // 2)

    def test_extended_headers_are_bounded_together_not_only_one_by_one(self) -> None:
        # tarfile reads a member's whole chain of extended headers before
        # yielding it, and keeps every global header's records for the rest
        # of the tar, so headers each under the bound must still be refused
        # once they pass it together.
        part = archive_to_tar.EXTENDED_HEADER_MAX // 2 + 1

        def header(kind: bytes) -> bytes:
            created: bytes = tarfile.TarInfo._create_pax_generic_header(  # type: ignore[attr-defined]
                {"comment": "\0" * part}, kind, "utf-8"
            )
            return created

        def member(name: str) -> bytes:
            built: bytes = tarfile.TarInfo(name).tobuf(tarfile.USTAR_FORMAT)
            return built

        cases = {
            "chained extended headers": header(tarfile.XHDTYPE)
            + header(tarfile.XHDTYPE)
            + member("tool-1.0/data"),
            "global headers across members": header(tarfile.XGLTYPE)
            + member("tool-1.0/one")
            + header(tarfile.XGLTYPE)
            + member("tool-1.0/two"),
        }
        for index, (case, body) in enumerate(cases.items()):
            with self.subTest(case):
                source = self.root / f"chained-{index}.tar.gz"
                source.write_bytes(gzip.compress(body + bytes(1024), mtime=0))
                with self.assertRaises(archive_to_tar.ArchiveError):
                    convert(source, self.root / f"chained-{index}.out")
        # A header per member, as a tar of long names has, is bounded per
        # member: together they pass the bound, and the tar converts.
        single = self.root / "single.tar.gz"
        single.write_bytes(
            gzip.compress(
                b"".join(
                    header(tarfile.XHDTYPE) + member(f"tool-1.0/data-{index}")
                    for index in range(3)
                )
                + bytes(1024),
                mtime=0,
            )
        )
        convert(single, self.root / "single.out")

    def test_sparse_members_are_refused_in_every_form(self) -> None:
        # tarfile builds a sparse member's map, from its header or its data,
        # before yielding it; nothing a tree here holds is sparse.
        old = self.root / "sparse-gnu.tar.gz"
        with tarfile.open(old, "w:gz", format=tarfile.GNU_FORMAT) as tar:
            info = tarfile.TarInfo("tool-1.0/sparse")
            info.type = tarfile.GNUTYPE_SPARSE
            tar.addfile(info)
        cases = {"GNU": old}
        for version, headers in (
            ("0.1", {"GNU.sparse.map": "0,1", "GNU.sparse.numblocks": "1"}),
            ("1.0", {"GNU.sparse.major": "1", "GNU.sparse.minor": "0"}),
        ):
            source = self.root / f"sparse-{version}.tar.gz"
            with tarfile.open(source, "w:gz", format=tarfile.PAX_FORMAT) as tar:
                info = tarfile.TarInfo("tool-1.0/sparse")
                info.size = 512
                info.pax_headers = {
                    "GNU.sparse.name": "tool-1.0/sparse",
                    "GNU.sparse.realsize": "1",
                    **headers,
                }
                tar.addfile(info, io.BytesIO(b"1\n0\n1\n".ljust(512, b"\0")))
            cases[f"PAX {version}"] = source
        for case, source in cases.items():
            with self.subTest(case), self.assertRaises(archive_to_tar.ArchiveError):
                convert(source, self.root / f"{source.name}.out")

    def test_the_remaining_extended_and_sparse_forms_are_refused(self) -> None:
        # A Solaris extended header and a GNU long link are read whole before
        # their member, like a PAX record or a GNU long name; a PAX sparse
        # 0.0 member's map comes from its records.
        big = archive_to_tar.EXTENDED_HEADER_MAX + 1
        solaris: bytes = tarfile.TarInfo._create_pax_generic_header(  # type: ignore[attr-defined]
            {"comment": "\0" * big}, tarfile.SOLARIS_XHDTYPE, "utf-8"
        )
        sources = {"Solaris extended header": self.root / "solaris.tar.gz"}
        sources["Solaris extended header"].write_bytes(
            gzip.compress(
                solaris
                + tarfile.TarInfo("tool-1.0/data").tobuf(tarfile.USTAR_FORMAT)
                + bytes(1024),
                mtime=0,
            )
        )
        sources["GNU long link"] = self.root / "longlink.tar.gz"
        with tarfile.open(
            sources["GNU long link"], "w:gz", format=tarfile.GNU_FORMAT
        ) as tar:
            info = tarfile.TarInfo("tool-1.0/alias")
            info.type = tarfile.SYMTYPE
            info.linkname = "a" * big
            tar.addfile(info)
        sources["PAX sparse 0.0"] = self.root / "sparse-0.0.tar.gz"
        with tarfile.open(
            sources["PAX sparse 0.0"], "w:gz", format=tarfile.PAX_FORMAT
        ) as tar:
            info = tarfile.TarInfo("tool-1.0/sparse")
            info.size = 1
            info.pax_headers = {
                "GNU.sparse.size": "1",
                "GNU.sparse.numblocks": "1",
                "GNU.sparse.offset": "0",
                "GNU.sparse.numbytes": "1",
            }
            tar.addfile(info, io.BytesIO(b"x"))
        for case, source in sources.items():
            with self.subTest(case):
                tracemalloc.start()
                try:
                    with self.assertRaises(archive_to_tar.ArchiveError):
                        convert(source, self.root / f"{source.name}.out")
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                self.assertLess(peak, big // 2)

    def test_names_and_links_longer_than_path_max_are_refused(self) -> None:
        # No platform here can create such a path, and each is held until
        # the tar is written.
        long = "tool-1.0/" + "a" * archive_to_tar.LINK_TARGET_MAX
        regular, link = tarfile.REGTYPE, tarfile.SYMTYPE
        for index, members in enumerate(
            (
                [(long, regular, 0o644, b"x", "")],
                [
                    (
                        "tool-1.0/alias",
                        link,
                        0o777,
                        b"",
                        "a/" * archive_to_tar.LINK_TARGET_MAX,
                    )
                ],
            )
        ):
            with (
                self.subTest(index=index),
                self.assertRaises(archive_to_tar.ArchiveError),
            ):
                self.convert_tar(members, f"long-{index}")
        with self.assertRaises(archive_to_tar.ArchiveError):
            self.convert_zip([(long, stat.S_IFREG | 0o644, b"x")], "long-zip")

    def test_retained_names_past_the_bound_are_refused(self) -> None:
        members = [
            (f"tool-1.0/{index:04d}", tarfile.REGTYPE, 0o644, b"", "")
            for index in range(4)
        ]
        original = archive_to_tar.RETAINED_MAX
        self.addCleanup(setattr, archive_to_tar, "RETAINED_MAX", original)
        # Each name is 13 bytes and is held twice, as itself and folded, so
        # four of them take 104 bytes.
        archive_to_tar.RETAINED_MAX = 13 * 2 * 4 - 1
        with self.assertRaises(archive_to_tar.ArchiveError):
            self.convert_tar(members, "retained")
        archive_to_tar.RETAINED_MAX = 13 * 2 * 4
        self.convert_tar(members, "retained-enough")

    def test_the_retained_bound_counts_bytes_not_characters(self) -> None:
        # Each name is 13 characters but 16 bytes, three of its characters
        # taking two, and its folded key, each `e` decomposed, 16 characters
        # but 19 bytes: 70 bytes for the pair, which counted by character
        # would fit a bound they pass by bytes.
        members = [
            (f"tool-1.0/{chr(0xE9) * 3}{index}", tarfile.REGTYPE, 0o644, b"", "")
            for index in range(2)
        ]
        original = archive_to_tar.RETAINED_MAX
        self.addCleanup(setattr, archive_to_tar, "RETAINED_MAX", original)
        archive_to_tar.RETAINED_MAX = 2 * (16 + 19) - 1
        with self.assertRaises(archive_to_tar.ArchiveError):
            self.convert_tar(members, "multibyte")
        archive_to_tar.RETAINED_MAX = 2 * (16 + 19)
        self.convert_tar(members, "multibyte-enough")

    def test_a_folded_key_that_lengthens_a_name_is_charged(self) -> None:
        # A precomposed Hangul syllable decomposes into jamo, doubling its
        # bytes, so a name of them holds more folded than as given.
        name = "tool-1.0/" + "\uac00" * 10
        given = len(name.encode())
        key = len(archive_to_tar.folded(name).encode())
        self.assertGreater(key, given)
        members = [(name, tarfile.REGTYPE, 0o644, b"", "")]
        original = archive_to_tar.RETAINED_MAX
        self.addCleanup(setattr, archive_to_tar, "RETAINED_MAX", original)
        archive_to_tar.RETAINED_MAX = given + key - 1
        with self.assertRaises(archive_to_tar.ArchiveError):
            self.convert_tar(members, "hangul")
        archive_to_tar.RETAINED_MAX = given + key
        self.convert_tar(members, "hangul-enough")

    def test_a_zip_directory_past_the_bound_is_refused_before_it_is_read(self) -> None:
        # zipfile reads a zip's whole central directory into one ZipInfo per
        # entry on opening it, so its size is checked from the end record
        # first; the bound is lowered here so the zip stays small.
        source = self.root / "wide.zip"
        with zipfile.ZipFile(source, "w") as archive:
            for index in range(20000):
                archive.writestr(f"tool-1.0/{index:05d}", b"")
        with zipfile.ZipFile(source) as archive:
            directory = archive.start_dir
        size = source.stat().st_size - directory - 22
        original = archive_to_tar.CENTRAL_DIRECTORY_MAX
        self.addCleanup(setattr, archive_to_tar, "CENTRAL_DIRECTORY_MAX", original)
        archive_to_tar.CENTRAL_DIRECTORY_MAX = size - 1
        tracemalloc.start()
        try:
            with self.assertRaises(archive_to_tar.ArchiveError):
                convert(source, self.root / "wide.tar.gz")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        # Opening it would have built 20000 ZipInfo objects, several MiB.
        self.assertLess(peak, 1 << 20)
        archive_to_tar.CENTRAL_DIRECTORY_MAX = size
        convert(source, self.root / "wide-enough.tar.gz")

    def test_a_link_whose_folded_name_lengthens_is_charged_twice(self) -> None:
        # A link's folded name is held twice, in the duplicate index and in
        # the link tree, so a lengthening one is charged in each place.
        name = "tool-1.0/" + "\uac00" * 10
        given = len(name.encode()) + len(b"x")
        key = len(archive_to_tar.folded(name).encode())
        members = [(name, tarfile.SYMTYPE, 0o777, b"", "x")]
        original = archive_to_tar.RETAINED_MAX
        self.addCleanup(setattr, archive_to_tar, "RETAINED_MAX", original)
        archive_to_tar.RETAINED_MAX = given + 2 * key - 1
        with self.assertRaises(archive_to_tar.ArchiveError):
            self.convert_tar(members, "hangul-link")
        archive_to_tar.RETAINED_MAX = given + 2 * key
        self.convert_tar(members, "hangul-link-enough")

    def test_a_multibyte_link_target_is_counted_in_bytes(self) -> None:
        # The target `\u00e9\u00e9\u00e9` is three characters and six bytes;
        # the ASCII name is held as itself and twice folded.
        name, target = "tool-1.0/a", "\u00e9" * 3
        held = 3 * len(name) + len(target.encode())
        members = [(name, tarfile.SYMTYPE, 0o777, b"", target)]
        original = archive_to_tar.RETAINED_MAX
        self.addCleanup(setattr, archive_to_tar, "RETAINED_MAX", original)
        archive_to_tar.RETAINED_MAX = held - 1
        with self.assertRaises(archive_to_tar.ArchiveError):
            self.convert_tar(members, "multibyte-target")
        archive_to_tar.RETAINED_MAX = held
        self.convert_tar(members, "multibyte-target-enough")

    def _zip64(self, name: str) -> Path:
        """A real zip with a ZIP64 end record: zipfile writes one past its file
        count limit, lowered here so a few entries pass it."""
        source = self.root / name
        with (
            mock.patch.object(zipfile, "ZIP_FILECOUNT_LIMIT", 1),
            zipfile.ZipFile(source, "w") as archive,
        ):
            for index in range(3):
                archive.writestr(f"tool-1.0/{index}", b"x")
        self.assertIn(b"PK\x06\x06", source.read_bytes())
        return source

    def test_a_zip64_directory_is_bounded_by_its_zip64_size(self) -> None:
        source = self._zip64("zip64.zip")
        data = source.read_bytes()
        with zipfile.ZipFile(source) as archive:
            size = data.rindex(b"PK\x06\x06") - archive.start_dir
        original = archive_to_tar.CENTRAL_DIRECTORY_MAX
        self.addCleanup(setattr, archive_to_tar, "CENTRAL_DIRECTORY_MAX", original)
        archive_to_tar.CENTRAL_DIRECTORY_MAX = size - 1
        with self.assertRaisesRegex(archive_to_tar.ArchiveError, "central directory"):
            convert(source, self.root / "zip64.tar.gz")
        archive_to_tar.CENTRAL_DIRECTORY_MAX = size
        convert(source, self.root / "zip64-enough.tar.gz")

    def test_a_corrupt_zip64_record_fails_as_the_cli_reports_a_bad_archive(
        self,
    ) -> None:
        # The preflight reads the end record before zipfile does, so its
        # failures must reach the CLI as the bad-archive exit, not a traceback.
        source = self._zip64("corrupt.zip")
        data = bytearray(source.read_bytes())
        locator = data.rindex(b"PK\x06\x07")
        # The locator's offset of the ZIP64 end record, now past the file.
        data[locator + 8 : locator + 16] = (len(data) * 2).to_bytes(8, "little")
        source.write_bytes(bytes(data))
        digest = hashlib.sha256(bytes(data)).hexdigest()
        status = archive_to_tar.main(
            [
                "--archive",
                str(source),
                "--digest",
                digest,
                "--tar-parts",
                str(self.root / "corrupt.parts"),
                "--part-bytes",
                str(1 << 20),
                "--tar-digest",
                str(self.root / "corrupt.sha256"),
            ]
        )
        self.assertEqual(status, 1)
        self.assertFalse((self.root / "corrupt.sha256").exists())

    def test_a_read_error_in_the_end_record_is_an_archive_error(self) -> None:
        # zipfile's end-record reader raises OSError when a read comes back
        # short; ZipFile turns that into a bad zip, and so must the preflight.
        source = self._zip64("unreadable.zip")
        with (
            mock.patch.object(
                zipfile, "_EndRecData", side_effect=OSError("short read")
            ),
            self.assertRaisesRegex(archive_to_tar.ArchiveError, "end record"),
        ):
            convert(source, self.root / "unreadable.tar.gz")

    def test_a_zip_directory_forging_a_small_entry_count_is_still_bounded(self) -> None:
        # zipfile parses records until the directory's declared size is used
        # up, whatever count the end record claims, so the bound comes from
        # the size: no more than one ZipInfo per smallest record.
        source = self.root / "forged.zip"
        with zipfile.ZipFile(source, "w") as archive:
            for index in range(200):
                archive.writestr(f"tool-1.0/{index:03d}", b"")
        data = bytearray(source.read_bytes())
        end = data.rindex(b"PK\x05\x06")
        # The end record's entry counts, on this disk and in all, claim one.
        data[end + 8 : end + 12] = (1).to_bytes(2, "little") * 2
        source.write_bytes(bytes(data))
        original = archive_to_tar.MEMBERS_MAX
        self.addCleanup(setattr, archive_to_tar, "MEMBERS_MAX", original)
        archive_to_tar.MEMBERS_MAX = 100
        # Refused by the directory's size before zipfile opens it, not by the
        # member ceiling after it has built every ZipInfo.
        with self.assertRaisesRegex(archive_to_tar.ArchiveError, "central directory"):
            convert(source, self.root / "forged.tar.gz")

    def test_an_archive_past_the_member_bound_is_refused(self) -> None:
        # Every member is held until the tar is written, so an archive of
        # near-empty members is capped; the bound is lowered here so the test
        # stays small.
        members = [
            (f"tool-1.0/{index}", tarfile.REGTYPE, 0o644, b"", "") for index in range(5)
        ]
        original = archive_to_tar.MEMBERS_MAX
        archive_to_tar.MEMBERS_MAX = 4
        self.addCleanup(setattr, archive_to_tar, "MEMBERS_MAX", original)
        with self.assertRaises(archive_to_tar.ArchiveError):
            self.convert_tar(members, "many")
        archive_to_tar.MEMBERS_MAX = 5
        self.convert_tar(members, "enough")


if __name__ == "__main__":
    unittest.main()
