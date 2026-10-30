"""Write a small tree as a reproducible tar, and its SHA-256 beside it."""

import hashlib
import io
import sys
import tarfile

FILES = {
    "tool-1/bin/hello": (b"#!/bin/sh\necho hello from the archive\n", 0o755),
    "tool-1/share/data.txt": (b"data\n", 0o644),
}


def main(archive: str, digest: str) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name in ("tool-1", "tool-1/bin", "tool-1/share"):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)
        for name, (content, mode) in FILES.items():
            info = tarfile.TarInfo(name)
            info.mode = mode
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    data = buffer.getvalue()
    with open(archive, "wb") as out:
        out.write(data)
    with open(digest, "w", encoding="ascii") as out:
        out.write(hashlib.sha256(data).hexdigest() + "\n")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
