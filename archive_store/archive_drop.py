"""What a declared_archive leaves out of its tree, and the pin on it.

A drop names members by patterns over their paths from the archive's root,
matched a component at a time, so `*` never spans a `/`; a member a pattern
matches is left out with everything beneath it. What it may leave out is
pinned: the names left out, one per line in the archive's order, must hash to
the declared digest, so a pin bump that adds, removes or renames anything
beneath a pattern fails the conversion rather than dropping content nobody
reviewed, as does a pattern that matches nothing. Where a drop sits among the
conversion's member checks is archive_to_tar.py's.
"""

from __future__ import annotations

import fnmatch
import hashlib
from collections.abc import Iterable
from pathlib import Path

# The names a mismatched drop shows, before the rest are counted.
SHOWN_DROPPED = 20


class DropError(ValueError):
    """A drop pattern is malformed, or what the drop left out is not what it
    declared."""


class Drop:
    """The members a declared_archive drops, the digest of the list they
    must make, and those it dropped."""

    def __init__(self, patterns: Iterable[str], expected_digest: str) -> None:
        """`expected_digest` is the listing's lowercase hex SHA-256."""
        self.expected_digest = expected_digest
        self.patterns: list[tuple[str, list[str]]] = []
        for pattern in patterns:
            parts = pattern.split("/")
            if (
                not pattern
                or "\0" in pattern
                or pattern.startswith("/")
                or any(part in ("", ".", "..") for part in parts)
            ):
                raise DropError(
                    f"drop pattern {pattern!r} is not a normalized relative path"
                )
            self.patterns.append((pattern, parts))
        self.matched: set[str] = set()
        self.dropped: list[str] = []

    def drops(self, name: str) -> bool:
        """Whether `name`, or a directory above it, matches a pattern."""
        parts = name.split("/")
        hit = False
        for pattern, pattern_parts in self.patterns:
            if len(parts) >= len(pattern_parts) and all(
                fnmatch.fnmatchcase(part, glob)
                for part, glob in zip(parts, pattern_parts, strict=False)
            ):
                self.matched.add(pattern)
                hit = True
        if hit:
            self.dropped.append(name)
        return hit

    def listing(self) -> str:
        """The names dropped, one per line in the archive's order."""
        return "".join(f"{name}\n" for name in self.dropped)

    def verify(self, archive: Path) -> None:
        """Raise DropError unless every pattern matched and the names
        dropped are the ones declared."""
        unmatched = [
            pattern for pattern, _ in self.patterns if pattern not in self.matched
        ]
        if unmatched:
            raise DropError(
                f"{archive} has no member any of these drop patterns match:"
                f" {', '.join(unmatched)}"
            )
        actual = hashlib.sha256(self.listing().encode("utf-8")).hexdigest()
        if actual != self.expected_digest:
            shown = self.dropped[:SHOWN_DROPPED]
            more = len(self.dropped) - len(shown)
            raise DropError(
                f"{archive}'s drop left out names hashing to {actual}, not the"
                f" declared {self.expected_digest}; review what it now leaves out"
                f" before declaring that digest:\n"
                + "".join(f"  {name}\n" for name in shown)
                + (f"  ... and {more} more\n" if more else "")
            )
