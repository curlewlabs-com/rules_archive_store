"""Builders for the archive conversion suites: archives made in memory, and a
test case whose scratch directory each conversion writes into. The suites read
real archives this module writes, never a model of them.
"""

from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import override

import archive_drop
import archive_parts
import archive_to_tar

type Member = tuple[str, int, bytes]
# A tar member: name, tarfile type, mode, content, and link name.
type TarMember = tuple[str, bytes, int, bytes, str]


def zip_bytes(members: list[Member]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, mode, content in members:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            archive.writestr(info, content)
    return buffer.getvalue()


def tar_bytes(members: list[TarMember], mode: str = "w:gz") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as archive:  # type: ignore[call-overload]
        for name, kind, permissions, content, link in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.mode = permissions
            info.linkname = link
            if kind == tarfile.REGTYPE:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            else:
                archive.addfile(info)
    return buffer.getvalue()


def _parts_of(output: Path) -> Path:
    return output.with_name(output.name + ".parts")


def convert(
    source: Path,
    output: Path,
    drop: archive_drop.Drop | None = None,
    strip_components: int = 0,
) -> None:
    """Convert the one-file archive `source` into the file `output`, through
    the part writer the CLI uses, with a part as large as any test's tar."""
    with archive_parts.PartWriter(_parts_of(output), 1 << 40) as writer:
        archive_to_tar.convert(
            archive_parts.parts_of([source]), writer, drop, strip_components
        )
    (part,) = _parts_of(output).iterdir()
    part.rename(output)
    _parts_of(output).rmdir()


def written(output: Path) -> bool:
    """Whether a conversion into `output` wrote anything."""
    return output.exists() or _parts_of(output).exists()


def joined(directory: Path) -> bytes:
    """The tar a conversion wrote into `directory` as parts, read whole."""
    return b"".join(path.read_bytes() for path in sorted(directory.iterdir()))


class ConversionTestCase(unittest.TestCase):
    @override
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)

    def convert_zip(self, members: list[Member], name: str = "tool") -> Path:
        source = self.root / f"{name}.zip"
        source.write_bytes(zip_bytes(members))
        output = self.root / f"{name}.tar.gz"
        convert(source, output)
        return output

    def convert_tar(
        self, members: list[TarMember], name: str, mode: str = "w:gz"
    ) -> Path:
        suffix = {
            "w:gz": ".tar.gz",
            "w:xz": ".tar.xz",
            "w:bz2": ".tar.bz2",
            "w:zst": ".tar.zst",
            "w": ".tar",
        }[mode]
        source = self.root / f"{name}{suffix}"
        source.write_bytes(tar_bytes(members, mode))
        output = self.root / f"{name}.out.tar.gz"
        convert(source, output)
        return output
