"""Write the archive probe's tree as a reproducible tar and zip, with digests.

The probe (`archive_probe` in probe.bzl) mounts this archive through the store
on each worker class, so the archive's bytes must not depend on the host that
built it: entries in a fixed order, with fixed times, owners and modes.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import stat
import tarfile
import zipfile
from pathlib import Path

ROOT = "probe-1"
FILES = {
    f"{ROOT}/bin/answer": (b"#!/bin/sh\necho mounted\n", 0o755),
    f"{ROOT}/share/payload.txt": (b"archive probe payload\n", 0o644),
    # Names differing only in case, as the NDK's netfilter headers do, which
    # the probe's declared archives drop, so the probe reads a tree that
    # converts only with its drop applied.
    f"{ROOT}/include/netfilter/xt_MARK.h": (b"upper\n", 0o644),
    f"{ROOT}/include/netfilter/xt_mark.h": (b"lower\n", 0o644),
}
DIRECTORIES = (
    ROOT,
    f"{ROOT}/bin",
    f"{ROOT}/share",
    f"{ROOT}/include",
    f"{ROOT}/include/netfilter",
)


def _entry(name: str, kind: bytes, mode: int, size: int = 0) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = mode
    info.size = size
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def archive_bytes() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name in DIRECTORIES:
            tar.addfile(_entry(name, tarfile.DIRTYPE, 0o755))
        for name, (content, mode) in FILES.items():
            tar.addfile(
                _entry(name, tarfile.REGTYPE, mode, len(content)), io.BytesIO(content)
            )
    return buffer.getvalue()


def zip_bytes() -> bytes:
    """The same tree as a zip, which declared_archive converts to tar."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in DIRECTORIES:
            directory = zipfile.ZipInfo(name + "/", date_time=(1980, 1, 1, 0, 0, 0))
            directory.external_attr = (stat.S_IFDIR | 0o755) << 16
            archive.writestr(directory, b"")
        for name, (content, mode) in FILES.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, content)
    return buffer.getvalue()


def _write(path: Path, digest: Path, content: bytes) -> None:
    path.write_bytes(content)
    digest.write_text(
        f"{hashlib.sha256(content).hexdigest()}  {path.name}\n", encoding="ascii"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--digest", type=Path, required=True)
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--zip-digest", type=Path, required=True)
    arguments = parser.parse_args()
    _write(arguments.archive, arguments.digest, archive_bytes())
    _write(arguments.zip, arguments.zip_digest, zip_bytes())


if __name__ == "__main__":
    main()
