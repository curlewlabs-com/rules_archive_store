"""An archive, or the tar converted from one, as ordered parts of one stream.

A remote cache refuses a blob larger than it is configured to hold, so an
archive larger than PART_BYTES (archive_parts.bzl) is downloaded, and its converted tar written, as parts. This
module names the parts, reads an archive's parts as the one seekable stream they
were cut from, and writes a stream as parts; what the stream holds is
archive_to_tar.py's to judge.
"""

from __future__ import annotations

import bisect
import hashlib
import io
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import IO, override

# A part of an archive is named for the whole archive, then this suffix and
# _PART_LETTERS lowercase letters, `aaa` first, as split names its outputs
# given that suffix length (`-a`): the repository rule that downloads a large
# archive splits it with split (archive_parts.bzl), and the conversion names
# its own parts the same way. The declared-tree check reads a file so named as
# one archive's (declared_trees.py), and the store helper reads the tar's parts
# in name order.
PART_SUFFIX = ".part-"
_PART_LETTERS = 3
# The name the converted tar's parts are named for: the parts' directory is
# what identifies the archive.
TAR_NAME = "archive.tar.gz"


class PartsError(ValueError):
    """The files are not one archive's parts, or a stream cannot be written as
    parts."""


def part_name(name: str, index: int) -> str:
    """The name of part `index` of the archive named `name`."""
    if not 0 <= index < 26**_PART_LETTERS:
        raise PartsError(f"{name} needs more than {26**_PART_LETTERS} parts")
    letters = ""
    for _ in range(_PART_LETTERS):
        index, letter = divmod(index, 26)
        letters = chr(ord("a") + letter) + letters
    return name + PART_SUFFIX + letters


class _Joined(io.RawIOBase):
    """Files read in order as one seekable stream."""

    def __init__(self, files: Sequence[Path]) -> None:
        super().__init__()
        with ExitStack() as opened:
            # Unbuffered, since the reader over this stream buffers it.
            self._streams = [
                opened.enter_context(path.open("rb", buffering=0)) for path in files
            ]
            self._closing = opened.pop_all()
        self._starts = [0]
        for stream in self._streams:
            self._starts.append(self._starts[-1] + stream.seek(0, io.SEEK_END))
        self._position = 0

    @override
    def readable(self) -> bool:
        return True

    @override
    def seekable(self) -> bool:
        return True

    @override
    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {
            io.SEEK_SET: 0,
            io.SEEK_CUR: self._position,
            io.SEEK_END: self._starts[-1],
        }[whence]
        if base + offset < 0:
            raise OSError(f"seek to {base + offset}, before the stream's start")
        self._position = base + offset
        return self._position

    @override
    def readinto(self, buffer: memoryview) -> int:  # type: ignore[override]
        if self._position >= self._starts[-1]:
            return 0
        # The last part starting at or before the position, which an empty
        # part never is while a later one starts at the same offset.
        index = bisect.bisect_right(self._starts, self._position) - 1
        stream = self._streams[index]
        stream.seek(self._position - self._starts[index])
        wanted = min(len(buffer), self._starts[index + 1] - self._position)
        read = stream.readinto(memoryview(buffer)[:wanted])
        self._position += read
        return read

    @override
    def close(self) -> None:
        if not self.closed:
            self._closing.close()
        super().close()


@dataclass(frozen=True)
class Parts:
    """An archive as the ordered files holding it: one file, or the parts a
    repository rule split it into. `name` is the archive as one file would be
    named, which says its format and names it in errors."""

    files: tuple[Path, ...]
    name: Path

    def open(self) -> io.BufferedReader:
        # One file reads as it always has, through its own buffer.
        if len(self.files) == 1:
            return self.files[0].open("rb")
        return io.BufferedReader(_Joined(self.files))


def parts_of(files: Sequence[Path]) -> Parts:
    """The archive `files` hold, or PartsError unless they are one file or
    its parts named in order from the first, so parts a declaration lists
    out of order fail by name before any is read."""
    if not files:
        raise PartsError("no archive was given")
    base, suffix, letters = files[0].name.rpartition(PART_SUFFIX)
    is_part = (
        bool(suffix)
        and len(letters) == _PART_LETTERS
        and letters.isascii()
        and letters.isalpha()
        and letters.islower()
    )
    if len(files) == 1 and not is_part:
        return Parts(tuple(files), files[0])
    expected = [part_name(base, index) for index in range(len(files))]
    if not is_part or [path.name for path in files] != expected:
        raise PartsError(
            f"an archive's parts must be named {', '.join(expected)} in that"
            f" order, not {', '.join(path.name for path in files)}"
        )
    return Parts(tuple(files), files[0].with_name(base))


class PartWriter(io.RawIOBase):
    """One stream written as parts of at most `part_bytes` each, named for
    TAR_NAME in `directory`, and hashed whole as it is written."""

    def __init__(self, directory: Path, part_bytes: int) -> None:
        super().__init__()
        if part_bytes < 1:
            raise PartsError(f"a part must hold at least a byte, not {part_bytes}")
        self._directory = directory
        self._part_bytes = part_bytes
        self._count = 0
        self._part: IO[bytes] | None = None
        self._held = 0
        self.digest = hashlib.sha256()

    @override
    def writable(self) -> bool:
        return True

    def _next(self) -> IO[bytes]:
        if self._part is None:
            # Made with the first byte, so a conversion refused before it
            # writes anything leaves nothing behind.
            self._directory.mkdir(parents=True, exist_ok=True)
        else:
            self._part.close()
        # Exclusive, so a part left from an earlier run is never appended to.
        self._part = (self._directory / part_name(TAR_NAME, self._count)).open("xb")
        self._count += 1
        self._held = 0
        return self._part

    @override
    def write(self, data: bytes) -> int:  # type: ignore[override]
        view = memoryview(data).cast("B")
        self.digest.update(view)
        while view:
            # Opened only when a byte needs it, so a tar ending at a part's
            # end leaves no empty part after it.
            part = self._part
            if part is None or self._held == self._part_bytes:
                part = self._next()
            taken = min(len(view), self._part_bytes - self._held)
            part.write(view[:taken])
            self._held += taken
            view = view[taken:]
        return len(memoryview(data).cast("B"))

    @override
    def close(self) -> None:
        if not self.closed and self._part is not None:
            self._part.close()
        super().close()
