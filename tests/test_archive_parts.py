"""An archive larger than the store's largest blob travels as ordered parts.

The conversion must read an archive's parts as the one archive they were cut
from, wherever the cuts fall, and write its tar as parts that join back into
the tar a single file would have given. A part missing, misnamed or out of
place must fail before any tree is built from it.
"""

from __future__ import annotations

import hashlib
import random
import stat
import tarfile
import unittest
from pathlib import Path

import archive_parts
import archive_to_tar
from archive_to_tar_test_support import (
    ConversionTestCase,
    Member,
    TarMember,
    convert,
    joined,
    tar_bytes,
    zip_bytes,
)

# Incompressible, so the compressed stream is as long as the member and every
# cut below lands inside a member's data rather than between members.
_PAYLOAD = random.Random(0).randbytes(6000)
_TAR: list[TarMember] = [
    ("tool-1.0", tarfile.DIRTYPE, 0o755, b"", ""),
    ("tool-1.0/one.bin", tarfile.REGTYPE, 0o644, _PAYLOAD, ""),
    ("tool-1.0/two.bin", tarfile.REGTYPE, 0o644, _PAYLOAD[::-1], ""),
    ("tool-1.0/alias", tarfile.SYMTYPE, 0o777, b"", "one.bin"),
]
_ZIP: list[Member] = [
    ("tool-1.0/", stat.S_IFDIR | 0o755, b""),
    ("tool-1.0/one.bin", stat.S_IFREG | 0o644, _PAYLOAD),
    ("tool-1.0/two.bin", stat.S_IFREG | 0o644, _PAYLOAD[::-1]),
]
# Smaller than either member, so each member spans a cut.
_PART_BYTES = 2500


class ArchivePartsTest(ConversionTestCase):
    def _split(self, name: str, content: bytes) -> list[Path]:
        """`content` cut into _PART_BYTES parts named as split(1) names them."""
        parts = []
        for index, start in enumerate(range(0, len(content), _PART_BYTES)):
            part = self.root / archive_parts.part_name(name, index)
            part.write_bytes(content[start : start + _PART_BYTES])
            parts.append(part)
        return parts

    def _main(self, parts: list[Path], digest: str, out: str) -> int:
        arguments = [
            argument for part in parts for argument in ("--archive", str(part))
        ]
        return archive_to_tar.main(
            [
                *arguments,
                "--digest",
                digest,
                "--tar-parts",
                str(self.root / out),
                "--part-bytes",
                str(_PART_BYTES),
                "--tar-digest",
                str(self.root / f"{out}.sha256"),
            ]
        )

    def test_parts_cut_mid_member_convert_to_the_tar_one_file_gives(self) -> None:
        for name, content in (
            ("tool.tar.gz", tar_bytes(_TAR)),
            # A zip is read from its end and seeked back, across the cuts.
            ("tool.zip", zip_bytes(_ZIP)),
        ):
            with self.subTest(name):
                whole = self.root / name
                whole.write_bytes(content)
                expected = self.root / f"{name}.expected"
                convert(whole, expected)
                parts = self._split(name, content)
                self.assertGreater(len(parts), 2)
                out = f"{name}.out"
                status = self._main(parts, hashlib.sha256(content).hexdigest(), out)
                self.assertEqual(status, 0)
                written = sorted((self.root / out).iterdir())
                self.assertGreater(len(written), 1)
                self.assertTrue(
                    all(part.stat().st_size <= _PART_BYTES for part in written)
                )
                self.assertEqual(
                    [part.name for part in written],
                    [
                        archive_parts.part_name(archive_parts.TAR_NAME, index)
                        for index in range(len(written))
                    ],
                )
                self.assertEqual(joined(self.root / out), expected.read_bytes())
                self.assertEqual(
                    (self.root / f"{out}.sha256").read_text(encoding="ascii"),
                    hashlib.sha256(expected.read_bytes()).hexdigest() + "\n",
                )

    def test_a_part_missing_or_out_of_place_converts_nothing(self) -> None:
        # The declared digest is the whole archive's, so a part lost or moved
        # anywhere must fail it, or fail on its name before anything is read.
        content = tar_bytes(_TAR)
        digest = hashlib.sha256(content).hexdigest()
        parts = self._split("tool.tar.gz", content)
        first, second = parts[0].read_bytes(), parts[1].read_bytes()
        swapped = self.root / "swapped"
        swapped.mkdir()
        swapped_parts = [swapped / part.name for part in parts]
        for part, target in zip(parts, swapped_parts, strict=True):
            target.write_bytes(part.read_bytes())
        swapped_parts[0].write_bytes(second)
        swapped_parts[1].write_bytes(first)
        cases = {
            # Named in order and joined, but short of the archive.
            "last part missing": parts[:-1],
            "middle part missing": [parts[0], *parts[2:]],
            "parts listed out of order": [parts[1], parts[0], *parts[2:]],
            "contents out of place under the right names": swapped_parts,
        }
        for index, (case, given) in enumerate(cases.items()):
            with self.subTest(case):
                out = f"broken-{index}"
                self.assertEqual(self._main(given, digest, out), 1)
                self.assertFalse((self.root / out).exists())
                self.assertFalse((self.root / f"{out}.sha256").exists())

    def test_a_tar_ending_on_a_part_boundary_leaves_no_empty_part(self) -> None:
        # The helper joins every part it finds; an empty last part is harmless
        # to the stream but would be a blob nobody needs.
        directory = self.root / "exact"
        with archive_parts.PartWriter(directory, 4) as writer:
            writer.write(b"abc")
            writer.write(b"defgh")
        self.assertEqual(
            [part.read_bytes() for part in sorted(directory.iterdir())],
            [b"abcd", b"efgh"],
        )
        self.assertEqual(
            writer.digest.hexdigest(), hashlib.sha256(b"abcdefgh").hexdigest()
        )

    def test_part_names_follow_split_and_are_bounded(self) -> None:
        # The repository rule's parts come from split, so the conversion's own
        # names and its check of the inputs' must agree with split's, the last
        # letter varying fastest.
        self.assertEqual(archive_parts.part_name("a.zip", 0), "a.zip.part-aaa")
        self.assertEqual(archive_parts.part_name("a.zip", 27), "a.zip.part-abb")
        self.assertEqual(archive_parts.part_name("a.zip", 26**3 - 1), "a.zip.part-zzz")
        with self.assertRaises(archive_parts.PartsError):
            archive_parts.part_name("a.zip", 26**3)

    def test_one_file_or_one_part_is_an_archive_and_a_stray_part_is_not(self) -> None:
        whole = archive_parts.parts_of([Path("tool.zip")])
        self.assertEqual(whole.name, Path("tool.zip"))
        single = archive_parts.parts_of([Path("d/tool.zip.part-aaa")])
        self.assertEqual(single.name, Path("d/tool.zip"))
        for case in (["tool.zip.part-aab"], ["tool.zip", "tool.zip.part-aab"], []):
            with (
                self.subTest(case),
                self.assertRaises(archive_parts.PartsError),
            ):
                archive_parts.parts_of([Path(name) for name in case])


if __name__ == "__main__":
    unittest.main()
