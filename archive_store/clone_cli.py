"""Run the strict archive clone from an action written in another language."""

from __future__ import annotations

import argparse
from pathlib import Path

import archive_clone


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--materialize-file-links", action="store_true")
    parser.add_argument("--writable-files", action="store_true")
    parser.add_argument("--keep-directory-modes", action="store_true")
    args = parser.parse_args()
    changed = archive_clone.clone(
        args.source,
        args.destination,
        materialize_file_links=args.materialize_file_links,
        writable_files=args.writable_files,
        writable_directories=not args.keep_directory_modes,
    )
    print(
        f"Cloned {args.source} to {args.destination}; writable entries: {changed}",
        flush=True,
    )


if __name__ == "__main__":
    main()
