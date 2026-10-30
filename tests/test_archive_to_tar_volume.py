"""archive_to_tar's case folding, checked against the names a Mac volume merges.

The volume is the oracle, so this suite observes the host it runs on rather
than any input Bazel can key a result by.
"""

from __future__ import annotations

import errno
import os
import sys
import unicodedata
import unittest
from pathlib import Path

import archive_to_tar
from archive_to_tar_test_support import ConversionTestCase


class CaseSensitiveVolumeError(AssertionError):
    """The volume keeps `xA` and `xa` apart, so it would nominate no pair a
    case-insensitive volume merges, and the corpus would pass unobserved."""


def _refuses_name(name: str) -> bool:
    """Whether the converter refuses a member of this name, by its own check."""
    try:
        archive_to_tar._normalized(name)
    except archive_to_tar.ArchiveError:
        return True
    return False


def _creates(path: Path) -> int | None:
    """The inode of `path`, created exclusively, or None if the volume reads
    the name as one it already holds."""
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    try:
        return os.fstat(descriptor).st_ino
    finally:
        os.close(descriptor)


def _require_case_insensitive(directory: Path) -> None:
    directory.mkdir()
    _creates(directory / "xa")
    if _creates(directory / "xA") is not None:
        raise CaseSensitiveVolumeError(
            f"{directory} is on a case-sensitive volume; the name check needs"
            " a case-insensitive one, as a Mac's default volume is"
        )


def _unfolded_pairs(directory: Path) -> list[tuple[str, str]]:
    """Each pair of code points the volume under `directory` merges as names
    but the fold keeps apart and the converter accepts, as U+ strings."""
    _require_case_insensitive(directory / "case")
    candidates = [
        chr(code)
        for code in range(0x20, 0x110000)
        if not 0xD800 <= code <= 0xDFFF
        and chr(code) not in "/\0"
        and (
            code < 0x10000
            or unicodedata.category(chr(code)) in ("Lu", "Ll", "Lt", "Lm")
            or unicodedata.category(chr(code))[0] in "MS"
        )
    ]
    names = directory / "names"
    names.mkdir()
    by_inode: dict[int, str] = {}
    missed = []
    for character in candidates:
        path = names / f"x{character}"
        try:
            inode = _creates(path)
        except OSError as error:
            # A name the volume will not store cannot be extracted there.
            # Any other failure would cut the corpus short, so it fails.
            if error.errno != errno.EILSEQ:
                raise
            continue
        if inode is not None:
            by_inode[inode] = character
            continue
        earlier = by_inode[path.stat().st_ino]
        pair = (earlier, character)
        refused = any(_refuses_name(f"tool-1.0/x{c}") for c in pair)
        if not refused and len({archive_to_tar.folded(c) for c in pair}) > 1:
            missed.append((f"U+{ord(earlier):04X}", f"U+{ord(character):04X}"))
    return missed


class MacVolumeNamesTest(ConversionTestCase):
    @unittest.skipUnless(sys.platform == "darwin", "the volume checked is a Mac's")
    def test_every_name_pair_a_mac_volume_merges_is_folded_or_refused(self) -> None:
        # folded() stands for a path's identity on a Mac volume, and a pair
        # the volume reads as one name but the fold keeps apart would let a
        # member past the beneath-a-link check there. So the volume itself
        # nominates the pairs, with no Unicode table of ours choosing them:
        # each name `x<c>` is created exclusively in one directory, and one
        # the volume refuses as existing names, by its inode, the earlier
        # character it merged with. Every pair must fold together, or hold a
        # character the converter refuses. This checks the case folding APFS
        # leaves unstated, per code point: each of the Basic Multilingual
        # Plane, and the cased and modifier letters, marks and symbols above
        # it. A name of several characters folds as its characters do, and
        # its normalization is APFS's documented NFD, which folded() applies
        # (its docstring has the argument).
        self.assertEqual(_unfolded_pairs(self.root), [])

    @unittest.skipUnless(sys.platform == "linux", "a Linux volume is case-sensitive")
    def test_a_case_sensitive_volume_fails_before_the_corpus_runs(self) -> None:
        # A case-sensitive volume merges no case pair, so without this the
        # oracle would pass on one having observed nothing. A Linux worker's
        # volume is a real case-sensitive one; a Mac's default is not.
        with self.assertRaises(CaseSensitiveVolumeError):
            _unfolded_pairs(self.root)
        self.assertFalse((self.root / "names").exists())


if __name__ == "__main__":
    unittest.main()
