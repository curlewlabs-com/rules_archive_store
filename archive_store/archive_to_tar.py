"""Convert a declared archive, tar or zip, into the tar the archive store expands.

The store's helper (archive_mount.sh) extracts only a tar this conversion wrote,
so what a tree may hold is decided here, on exact member metadata, before any
tree exists, and never by how a platform's tar or unzip treats links.
declared_archive (archives.bzl) runs this once per archive version, as a cached
action.

It checks the archive against its declared digest, a SHA-256 or the SHA-512
Subresource Integrity value an npm lock pins, and refuses:

- a member that would land outside the tree, or beneath one of the archive's
  own links;
- a link whose target leaves the tree;
- a hard link to anything but a regular member named before it;
- a device, FIFO or other special member;
- a path named twice, except a tar's directory or regular file named again
  unchanged, which npm's own tarballs do (`package/./dist/index.js` beside
  `package/dist/index.js`);
- a NUL in a name or link, and anything but a directory at the tree's root;
- a sparse member, in any of its forms;
- extended headers past EXTENDED_HEADER_MAX, for one member or for the tar's
  global ones, more than MEMBERS_MAX members, or names and link targets past
  RETAINED_MAX in all;
- a name or link target longer than PATH_MAX;
- a name holding a control character, bytes that are not UTF-8, or a
  character this interpreter's Unicode tables do not know;
- a name a case-insensitive volume would merge with another member's, or that
  lies beneath a link of another case.

A declared_archive may name members to drop (--drop), each a pattern over
member paths from the archive's root, matched a component at a time, so `*`
never spans a `/`. A member a pattern matches is left out with everything
beneath it. Each member is read before it is dropped, so its name, type,
compression, sparse form and the size bounds are checked as any member's are;
the drop comes before the duplicate and case-fold checks and before the path
and link checks (_check), so a dropped link's target is not validated, which
is safe only because nothing dropped is written. Keep it there: a drop moved
ahead of reading a member would skip checks every name owes. What it may
leave out is pinned by --dropped-digest (archive_drop.py), and the names are
written to --dropped. Dropping is only for members that cannot exist on a
case-insensitive volume and that nothing built reads.

A tar may have its members' leading path components stripped
(--strip-components), as npm strips a package tarball's top directory whatever
it is named. Each member is then judged, dropped and written by its stripped
name, and a hard link by its stripped target. A directory left with no name is
the tree's root, and any other such member is refused.

It writes a tar whose bytes depend only on the archive's: members in the
archive's order, with fixed times and owners and permission bits only, the set-ID
and sticky bits dropped. The tar's digest goes beside it, computed from the
bytes written, which is the digest consumers key the store by.

An archive arrives, and its tar leaves, as ordered parts (archive_parts.py),
since a store block is also the largest blob the store accepts. The parts are
read as one stream, and the declared digest is checked over that whole stream;
a small archive is one file, or one part.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import bz2
import gzip
import hashlib
import io
import lzma
import posixpath
import stat
import sys
import tarfile
import unicodedata
import zipfile
from collections.abc import Callable, Iterable, Iterator
from compression import zstd
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import IO

from archive_drop import Drop, DropError
from archive_parts import Parts, PartsError, PartWriter, parts_of

_CHUNK = 1 << 20
# A member's name and a link's target are paths, which no platform this runs
# on lets exceed PATH_MAX; a longer one is refused, and a zip link's payload
# before it is read into memory.
LINK_TARGET_MAX = 4096
_HARDLINK = -1
# Unicode categories a member name may not hold (_normalized): control
# characters, lone surrogates, which stand for bytes that are not UTF-8, and
# code points this interpreter's tables do not assign.
_UNJUDGED_CATEGORIES = frozenset({"Cc", "Cs", "Cn"})
# Marks a link's own node in _check's tree of link components; no path
# component can hold a NUL.
_LINK_END = "\0"
# A tar's extended header (a PAX record set, or a GNU long name or link) is
# read whole before its member is seen, so one larger than this is refused
# unread: a real one holds a path, a link and a few attributes.
EXTENDED_HEADER_MAX = 1 << 20
# Every member is held, small, until the tar is written. An order of magnitude
# above the largest tree the graph declares, so only an archive of near-empty
# members, which a high compression ratio makes small on disk, reaches it.
MEMBERS_MAX = 1_000_000
# The names and link targets held until the tar is written, in bytes: these
# ceilings bound a worker's memory against a malformed archive, and this one
# sits far above any real tree's, which spends well under a hundred bytes a
# member.
RETAINED_MAX = 256 << 20
# A zip's central directory, which zipfile reads whole and turns into one
# ZipInfo per entry before anything here sees a member, so its size is checked
# first. A real SDK zip's directory is a few MiB.
CENTRAL_DIRECTORY_MAX = 64 << 20
# A central directory record's fixed part, which the ZIP specification sets.
_CENTRAL_RECORD_MIN = 46
_EXTENDED_TYPES = frozenset(
    {
        tarfile.XHDTYPE,
        tarfile.XGLTYPE,
        tarfile.SOLARIS_XHDTYPE,
        tarfile.GNUTYPE_LONGNAME,
        tarfile.GNUTYPE_LONGLINK,
    }
)
# Each compressed tar is opened by its module's own file class, which bounds
# every read's output, rather than by tarfile's stream reader, which
# decompresses each buffered chunk with no limit: a high-ratio tar would expand
# that chunk past a worker's memory. tarfile then reads the plain stream.
_DECOMPRESSORS: dict[str, Callable[[io.BufferedReader], io.BufferedIOBase] | None] = {
    ".tar": None,
    ".tar.gz": lambda raw: gzip.GzipFile(fileobj=raw, mode="rb"),
    ".tgz": lambda raw: gzip.GzipFile(fileobj=raw, mode="rb"),
    ".tar.xz": lzma.LZMAFile,
    ".tar.bz2": bz2.BZ2File,
    ".tar.zst": zstd.ZstdFile,
}
# The compression methods whose reads CPython's zipfile bounds: stored data is
# copied, and deflate is decompressed with an output limit. Its bzip2, LZMA
# and Zstandard paths decompress a whole input chunk unbounded, so a
# high-ratio member in one could expand past a worker's memory in one read.
_BOUNDED_ZIP_METHODS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})


class ArchiveError(ValueError):
    """The archive cannot become a tree safely."""


def digest_of(paths: Iterable[Path], algorithm: str) -> str:
    """The digest of the files' bytes read in order, as one stream."""
    digest = hashlib.new(algorithm)
    for path in paths:
        with path.open("rb") as stream:
            while chunk := stream.read(_CHUNK):
                digest.update(chunk)
    return digest.hexdigest()


def declared_digest(value: str) -> str:
    """A digest given inline, or as `@PATH` to a file whose first word it is."""
    if value.startswith("@"):
        value = Path(value[1:]).read_text(encoding="ascii").split()[0]
    if len(value) != 64 or value.strip("0123456789abcdef"):
        raise ArchiveError(f"digest {value!r} is not lowercase hex SHA-256")
    return value


def archive_digest(value: str) -> tuple[str, str]:
    """The algorithm and lowercase hex digest an archive is declared under: a
    SHA-256 as declared_digest reads one, or `sha512-<base64>`, the Subresource
    Integrity form in which an npm lock pins a tarball."""
    if not value.startswith("sha512-"):
        return "sha256", declared_digest(value)
    try:
        raw = base64.b64decode(value.removeprefix("sha512-"), validate=True)
    except binascii.Error as error:
        raise ArchiveError(f"integrity {value!r} is not base64") from error
    if len(raw) != hashlib.sha512().digest_size:
        raise ArchiveError(f"integrity {value!r} is not a SHA-512")
    return "sha512", raw.hex()


def _normalized(name: str) -> str:
    """A member path in normal form, or ArchiveError if it leaves the tree.
    `.` is the tree's root."""
    # A tar field ends at its first NUL, so a name holding one would be
    # checked as one path and written as another.
    if "\0" in name:
        raise ArchiveError(f"member {name!r} holds a NUL")
    if len(name.encode("utf-8", errors="surrogateescape")) > LINK_TARGET_MAX:
        raise ArchiveError(f"member name {name[:64]!r}... is longer than PATH_MAX")
    # The names folded() can judge: a character newer than this interpreter's
    # Unicode tables has no folding here while a newer volume may merge it with
    # one they know; a control character no tree here holds; and bytes that
    # are not UTF-8, which tarfile hands over as lone surrogates and APFS will
    # not store.
    refused = next(
        (ch for ch in name if unicodedata.category(ch) in _UNJUDGED_CATEGORIES), None
    )
    if refused is not None:
        raise ArchiveError(
            f"member {name!r} holds U+{ord(refused):04X}, a control character,"
            " a byte that is not UTF-8, or a character this interpreter's"
            " Unicode tables do not know, so how a volume folds it cannot be"
            " checked"
        )
    stripped = name.rstrip("/")
    if not stripped or stripped.startswith("/"):
        raise ArchiveError(f"member {name!r} is not a relative path")
    normal = posixpath.normpath(stripped)
    if normal == ".." or normal.startswith("../"):
        raise ArchiveError(f"member {name!r} leaves the tree")
    return normal


@dataclass(frozen=True, slots=True)
class Member:
    """One member as the tar will hold it."""

    name: str
    # stat.S_IFDIR, S_IFREG or S_IFLNK, or _HARDLINK.
    kind: int
    mode: int
    size: int = 0
    # A symlink's target, or the normalized name a hard link names.
    link: str = ""
    # A tar's regular member's SHA-256, so a repeat of it can be judged
    # unchanged. Its size is fixed, so MEMBERS_MAX bounds what these hold.
    content: bytes = b""


type _Node = dict[str, _Node]


def folded(name: str) -> str:
    """A path's identity on a case-insensitive macOS volume: Unicode's
    canonical caseless form, NFD(casefold(NFD(name))).

    The normalization is APFS's own: the Apple File System Reference hashes a
    name in NFD (j_drec_hashed_key_t). Its case folding it leaves unstated, so
    the macOS test asks the volume which names it merges, per code point, and
    requires the fold to merge each pair; full case folding, which casefold()
    applies, merges at least what simple folding does. Unicode's stability
    policies fix a character's folding and decomposition once it is assigned,
    so a volume on a newer Unicode treats every character these tables know
    the same way, and _normalized refuses the rest."""
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", name).casefold())


def _check(members: list[Member | None]) -> None:
    """Raise ArchiveError unless every member stays in the tree. A None is a
    repeated directory, which the tar leaves out."""
    kinds: dict[str, int] = {}
    for member in members:
        if member is None:
            continue
        if member.kind == _HARDLINK and kinds.get(member.link) != stat.S_IFREG:
            raise ArchiveError(
                f"hard link {member.name!r} names {member.link!r},"
                " which is not a regular member before it"
            )
        if "\0" in member.link:
            raise ArchiveError(f"link {member.name!r} names a target holding a NUL")
        if len(member.link.encode("utf-8", errors="surrogateescape")) > LINK_TARGET_MAX:
            raise ArchiveError(
                f"link {member.name!r} has a target longer than PATH_MAX"
            )
        if member.kind == stat.S_IFLNK:
            target = member.link
            resolved = posixpath.normpath(
                posixpath.join(posixpath.dirname(member.name), target)
            )
            if (
                not target
                or target.startswith("/")
                or resolved == ".."
                or resolved.startswith("../")
            ):
                raise ArchiveError(
                    f"link {member.name!r} -> {target!r} leaves the tree"
                )
        kinds[member.name] = member.kind
    # The links as a tree of path components, so each name is walked once,
    # component by component, rather than hashed again at every ancestor.
    # Each component is folded: on a case-insensitive volume `Alias` and
    # `alias` are one directory entry, so a member beneath either lies beneath
    # the link.
    links: _Node = {}
    for name, kind in kinds.items():
        if kind == stat.S_IFLNK:
            node = links
            for part in folded(name).split("/"):
                node = node.setdefault(part, {})
            node[_LINK_END] = {}
    for name in kinds:
        node = links
        parts = name.split("/")
        for depth, part in enumerate(folded(name).split("/")[:-1]):
            node = node.get(part, {})
            if _LINK_END in node:
                above = "/".join(parts[: depth + 1])
                raise ArchiveError(f"member {name!r} lies beneath link {above!r}")
            if not node:
                break


def _deduplicated(
    members: Iterator[Member],
    archive: Path,
    *,
    unchanged_repeats: bool,
    drop: Drop | None = None,
) -> list[Member | None]:
    """The members in order, a root directory, an identical repeated
    directory or regular member (where those are allowed) and a dropped
    member as None, or ArchiveError for any other path named twice."""
    # By folded name, so members a case-insensitive volume would extract to
    # one path collide here rather than overwriting each other there.
    seen: dict[str, Member] = {}
    listed: list[Member | None] = []
    retained = 0
    for member in members:
        if len(listed) >= MEMBERS_MAX:
            raise ArchiveError(f"{archive} holds more than {MEMBERS_MAX} members")
        retained += _encoded_length(member.name) + _encoded_length(member.link)
        if retained > RETAINED_MAX:
            raise ArchiveError(
                f"{archive}'s names and link targets pass the {RETAINED_MAX}-byte bound"
            )
        if drop is not None and member.name != "." and drop.drops(member.name):
            listed.append(None)
            continue
        if member.name == ".":
            # The tree's root is the expansion's own directory, which only a
            # directory entry may name; its mode is the helper's to set.
            if member.kind != stat.S_IFDIR:
                raise ArchiveError(f"{archive} names its root as a non-directory")
            listed.append(None)
            continue
        key = folded(member.name)
        # The folded key is held too, and folding can lengthen a name; a
        # link's is held again, as components of _check's link tree.
        retained += _encoded_length(key) * (2 if member.kind == stat.S_IFLNK else 1)
        if retained > RETAINED_MAX:
            raise ArchiveError(
                f"{archive}'s names and link targets pass the {RETAINED_MAX}-byte bound"
            )
        earlier = seen.get(key)
        if earlier is None:
            seen[key] = member
            listed.append(member)
        elif (
            unchanged_repeats
            and earlier == member
            and member.kind in (stat.S_IFDIR, stat.S_IFREG)
        ):
            listed.append(None)
        else:
            raise ArchiveError(
                f"{archive} names {member.name!r} more than once, counting names"
                " that differ only in case or Unicode normalization as one"
            )
    if drop is not None:
        drop.verify(archive)
    return listed


def _encoded_length(text: str) -> int:
    return len(text.encode("utf-8", errors="surrogateescape"))


def _preflight_zip(archive: Parts) -> None:
    """Refuse a zip whose central directory passes the bounds, before
    zipfile reads it. The record read is zipfile's own (ZIP64 included), so
    the size checked is the size it would read."""
    path = archive.name
    # zipfile reports a read error here as a bad zip, which ZipFile does too.
    try:
        with archive.open() as stream:
            record = zipfile._EndRecData(stream)  # type: ignore[attr-defined]
    except OSError as error:
        raise ArchiveError(f"{path}'s end record cannot be read: {error}") from error
    if not record:
        raise ArchiveError(f"{path} is not a zip")
    size: int = record[zipfile._ECD_SIZE]  # type: ignore[attr-defined]
    # zipfile parses records until the directory's declared size is consumed,
    # whatever entry count the record claims, so the size is what bounds the
    # ZipInfos it builds: no more than one per smallest record. Holding it to
    # MEMBERS_MAX of those caps them at MEMBERS_MAX, a count that may lie.
    bound = min(CENTRAL_DIRECTORY_MAX, MEMBERS_MAX * _CENTRAL_RECORD_MIN)
    if size > bound:
        raise ArchiveError(
            f"{path}'s central directory is {size} bytes, over the {bound}-byte bound"
        )


def _zip_kind(info: zipfile.ZipInfo) -> int:
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        return stat.S_IFLNK
    if info.is_dir() or stat.S_ISDIR(mode):
        return stat.S_IFDIR
    return stat.S_IFREG


def _zip_mode(info: zipfile.ZipInfo, kind: int) -> int:
    mode = (info.external_attr >> 16) & 0o777
    if kind == stat.S_IFDIR:
        return mode or 0o755
    # A zip made off Unix carries no mode; its files are then plain data.
    return mode or 0o644


def _tar_suffix(path: Path) -> str:
    name = path.name.lower()
    # Longest first, so `.tar.gz` is never read as `.tar`'s.
    for suffix in sorted(_DECOMPRESSORS, key=len, reverse=True):
        if name.endswith(suffix):
            return suffix
    raise ArchiveError(f"{path} is neither a zip nor a tar")


@contextmanager
def _plain_tar(archive: Parts) -> Iterator[io.BufferedIOBase]:
    """The tar `archive` holds, decompressed with bounded reads."""
    decompressor = _DECOMPRESSORS[_tar_suffix(archive.name)]
    with archive.open() as raw:
        if decompressor is None:
            yield raw
            return
        with decompressor(raw) as stream:
            yield stream


def _zip_members(archive: zipfile.ZipFile) -> Iterator[Member]:
    for info in archive.infolist():
        name = _normalized(info.filename)
        if info.compress_type not in _BOUNDED_ZIP_METHODS:
            raise ArchiveError(
                f"member {name!r} uses compression method {info.compress_type},"
                " which is read unbounded; only stored and deflated members are"
                " accepted"
            )
        kind = _zip_kind(info)
        link = ""
        if kind == stat.S_IFLNK:
            if info.file_size > LINK_TARGET_MAX:
                raise ArchiveError(
                    f"link {name!r} has a {info.file_size}-byte target,"
                    f" over the {LINK_TARGET_MAX}-byte bound"
                )
            # Read with a limit, not whole: a zip may declare a small size
            # while its deflated data expands much further, and an unbounded
            # read decompresses all of it first.
            with archive.open(info) as stream:
                payload = stream.read(LINK_TARGET_MAX + 1)
            if len(payload) > LINK_TARGET_MAX:
                raise ArchiveError(f"link {name!r} has a target over the bound")
            link = payload.decode("utf-8")
        yield Member(name, kind, _zip_mode(info, kind), info.file_size, link)


def _stripped(name: str, components: int, *, directory: bool) -> str:
    """`name`, normalized, without its first `components` components."""
    parts = name.split("/")
    if len(parts) > components:
        return "/".join(parts[components:])
    if directory or name == ".":
        return "."
    raise ArchiveError(
        f"member {name!r} holds no path once {components} components are stripped"
    )


def _tar_member(info: tarfile.TarInfo, strip_components: int) -> Member:
    name = _stripped(_normalized(info.name), strip_components, directory=info.isdir())
    mode = info.mode & 0o777
    if info.isdir():
        return Member(name, stat.S_IFDIR, mode or 0o755)
    if info.issym():
        return Member(name, stat.S_IFLNK, 0o777, link=info.linkname)
    if info.islnk():
        link = _stripped(_normalized(info.linkname), strip_components, directory=False)
        return Member(name, _HARDLINK, mode, link=link)
    if info.isreg():
        return Member(name, stat.S_IFREG, mode or 0o644, info.size)
    raise ArchiveError(f"member {info.name!r} is not a file, directory or link")


def _bounded_tarinfo() -> type[tarfile.TarInfo]:
    """A TarInfo class for one read of one tar, which refuses extended headers
    past the bound before tarfile reads them, and every sparse form."""
    # Bytes of extended header read since the last member, and of global
    # headers in the whole tar: tarfile reads a member's chain of headers
    # before yielding it, and keeps every global header's records.
    read = {"chain": 0, "global": 0}

    class BoundedTarInfo(tarfile.TarInfo):
        # The hook every header passes through, chained PAX headers included,
        # before tarfile reads an extended record or a sparse map; the pinned
        # interpreter keeps its name, and tests fail if it is no longer called.
        def _proc_member(self, archive: tarfile.TarFile) -> tarfile.TarInfo:
            if self.type == tarfile.GNUTYPE_SPARSE:
                raise ArchiveError(f"member {self.name!r} is sparse")
            if self.type in _EXTENDED_TYPES:
                read["chain"] += self.size
                if self.type == tarfile.XGLTYPE:
                    read["global"] += self.size
                if max(read.values()) > EXTENDED_HEADER_MAX:
                    raise ArchiveError(
                        f"{self.name!r} takes the tar's extended headers past"
                        f" the {EXTENDED_HEADER_MAX}-byte bound"
                    )
            else:
                read["chain"] = 0
            member = super()._proc_member(archive)  # type: ignore[misc]
            assert isinstance(member, tarfile.TarInfo)
            return member

        # A PAX sparse member's map is read after its records, from its data in
        # format 1.0, so each format is refused where tarfile would build it.
        def _proc_gnusparse_00(self, *_arguments: object) -> None:
            raise ArchiveError(f"member {self.name!r} is sparse")

        def _proc_gnusparse_01(self, *_arguments: object) -> None:
            raise ArchiveError(f"member {self.name!r} is sparse")

        def _proc_gnusparse_10(self, *_arguments: object) -> None:
            raise ArchiveError(f"member {self.name!r} is sparse")

    return BoundedTarInfo


def _tar_members(source: Parts, strip_components: int) -> Iterator[Member]:
    with (
        _plain_tar(source) as raw,
        tarfile.open(fileobj=raw, mode="r|", tarinfo=_bounded_tarinfo()) as archive,
    ):
        for info in archive:
            member = _tar_member(info, strip_components)
            if member.kind == stat.S_IFREG:
                # The stream reads every member's data to reach the next
                # header anyway, so hashing it costs no further read.
                stream = archive.extractfile(info)
                if stream is None:
                    raise ArchiveError(f"member {info.name!r} has no data")
                digest = hashlib.sha256()
                while chunk := stream.read(_CHUNK):
                    digest.update(chunk)
                member = replace(member, content=digest.digest())
            yield member


def _entry(member: Member) -> tarfile.TarInfo:
    entry = tarfile.TarInfo(member.name)
    entry.type = {
        stat.S_IFDIR: tarfile.DIRTYPE,
        stat.S_IFLNK: tarfile.SYMTYPE,
        stat.S_IFREG: tarfile.REGTYPE,
        _HARDLINK: tarfile.LNKTYPE,
    }[member.kind]
    entry.mode = member.mode
    entry.mtime = 0
    entry.uid = entry.gid = 0
    entry.uname = entry.gname = ""
    if member.kind in (stat.S_IFLNK, _HARDLINK):
        entry.linkname = member.link
    if member.kind == stat.S_IFREG:
        entry.size = member.size
    return entry


type Content = Callable[[], AbstractContextManager[IO[bytes]]]


def _write(
    members: list[Member | None],
    contents: Iterator[Content | None],
    tar_out: io.RawIOBase,
) -> None:
    """Write `members` to `tar_out`, each regular one's bytes from the opener
    `contents` yields in step with it."""
    # An empty name and mtime=0 keep the gzip header, like every tar entry,
    # independent of where and when the conversion ran.
    with (
        gzip.GzipFile(filename="", fileobj=tar_out, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for member, content in zip(members, contents, strict=True):
            if member is None:
                continue
            entry = _entry(member)
            if member.kind != stat.S_IFREG:
                tar.addfile(entry)
                # The writer keeps each entry it wrote, which nothing here
                # reads; getmembers() is that list itself.
                tar.getmembers().clear()
                continue
            assert content is not None
            # Streamed: addfile copies exactly `size` bytes, so a member is
            # never held whole.
            with content() as stream:
                tar.addfile(entry, stream)
            tar.getmembers().clear()


def _opened(stream: IO[bytes]) -> Content:
    """An opener for a stream already open, which its archive closes."""
    return lambda: nullcontext(stream)


def _convert_zip(source: Parts, tar_out: io.RawIOBase, drop: Drop | None) -> None:
    _preflight_zip(source)
    with source.open() as stream, zipfile.ZipFile(stream) as archive:
        members = _deduplicated(
            _zip_members(archive), source.name, unchanged_repeats=False, drop=drop
        )
        _check(members)
        # A stored or deflated member's reader serves bounded reads, at most
        # the member's declared size in all.
        contents = (partial(archive.open, info) for info in archive.infolist())

        _write(members, contents, tar_out)


def checked_tar_members(
    source: Parts, drop: Drop | None = None, strip_components: int = 0
) -> list[Member | None]:
    """Every member of the tar `source`, in order, judged as the conversion
    judges it, under every bound here, with each that leaves nothing to
    write as None; or ArchiveError. tar_contents yields in step with it."""
    # A tar may name a directory again, as an appended one does, or a file
    # again with the same bytes, as npm's tarballs do; the repeat changes
    # nothing the tree holds.
    members = _deduplicated(
        _tar_members(source, strip_components),
        source.name,
        unchanged_repeats=True,
        drop=drop,
    )
    _check(members)
    return members


def tar_contents(source: Parts) -> Iterator[Content | None]:
    """An opener for each regular member's bytes, and None for any other, in
    step with checked_tar_members."""
    with (
        _plain_tar(source) as raw,
        tarfile.open(fileobj=raw, mode="r|", tarinfo=_bounded_tarinfo()) as archive,
    ):
        for info in archive:
            if not info.isreg():
                yield None
                continue
            stream = archive.extractfile(info)
            if stream is None:
                raise ArchiveError(f"member {info.name!r} has no data")
            yield _opened(stream)


def _convert_tar(
    source: Parts, tar_out: io.RawIOBase, drop: Drop | None, strip_components: int
) -> None:
    # Read twice as a stream: once to check every member before anything is
    # written, once to copy, so neither the tar nor a member is held whole and
    # a compressed tar is never seeked.
    members = checked_tar_members(source, drop, strip_components)
    _write(members, tar_contents(source), tar_out)


def convert(
    archive: Parts,
    tar_out: io.RawIOBase,
    drop: Drop | None = None,
    strip_components: int = 0,
) -> None:
    """Write `archive`'s tree to `tar_out` as a gzipped tar, leaving out what
    `drop` matches and the first `strip_components` components of each tar
    member's name, or raise ArchiveError."""
    name = archive.name.name.lower()
    if name.endswith(".zip"):
        if strip_components:
            raise ArchiveError(f"{archive.name}: only a tar's components are stripped")
        _convert_zip(archive, tar_out, drop)
    elif name.endswith(tuple(_DECOMPRESSORS)):
        _convert_tar(archive, tar_out, drop, strip_components)
    else:
        raise ArchiveError(f"{archive.name} is neither a zip nor a tar")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive",
        type=Path,
        action="append",
        required=True,
        help="the archive, or each of its parts in order",
    )
    parser.add_argument(
        "--digest",
        required=True,
        help="the archive's SHA-256, @PATH to a file holding it, or its"
        " sha512- Subresource Integrity value",
    )
    parser.add_argument(
        "--tar-parts",
        type=Path,
        required=True,
        help="the directory to write the tar's parts into",
    )
    parser.add_argument(
        "--part-bytes", type=int, required=True, help="the largest part to write"
    )
    parser.add_argument("--tar-digest", type=Path, required=True)
    parser.add_argument(
        "--strip-components",
        type=int,
        default=0,
        help="leading components to strip from each tar member's name",
    )
    parser.add_argument(
        "--drop",
        action="append",
        default=[],
        help="a member pattern to leave out, with everything beneath it",
    )
    parser.add_argument(
        "--dropped", type=Path, help="where to list the members --drop left out"
    )
    parser.add_argument(
        "--dropped-digest",
        help="the SHA-256 the list of members --drop leaves out must have",
    )
    arguments = parser.parse_args(argv)
    if (
        not bool(arguments.drop)
        == bool(arguments.dropped)
        == bool(arguments.dropped_digest)
    ):
        parser.error("--drop, --dropped and --dropped-digest go together")
    if arguments.strip_components < 0:
        parser.error("--strip-components cannot be negative")
    try:
        algorithm, expected = archive_digest(arguments.digest)
        archive = parts_of(arguments.archive)
        actual = digest_of(archive.files, algorithm)
        if actual != expected:
            raise ArchiveError(
                f"{archive.name} has digest {actual}, but it is declared as {expected}"
            )
        drop = (
            Drop(arguments.drop, declared_digest(arguments.dropped_digest))
            if arguments.drop
            else None
        )
        # Unbuffered: the gzip writer buffers what it compresses, and passes
        # on only compressed output.
        with PartWriter(arguments.tar_parts, arguments.part_bytes) as writer:
            convert(archive, writer, drop, arguments.strip_components)
    except (
        ArchiveError,
        DropError,
        PartsError,
        zipfile.BadZipFile,
        tarfile.TarError,
        UnicodeDecodeError,
    ) as error:
        print(f"archive_to_tar: error: {error}", file=sys.stderr)
        return 1
    if drop is not None:
        arguments.dropped.write_text(drop.listing(), encoding="utf-8")
    arguments.tar_digest.write_text(writer.digest.hexdigest() + "\n", encoding="ascii")
    return 0


if __name__ == "__main__":
    sys.exit(main())
