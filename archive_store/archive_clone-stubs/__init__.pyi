# The types of archive_clone.py, for a caller whose checker reads that module
# where it is installed. PEP 561 can mark a package typed but not a single-file
# module, so its types ship as this stub-only package beside it, which mypy and
# pyright both read from an installed search path. tools/check_typed_caller.py
# holds it equal to the module.

from pathlib import Path

def clone(
    mount: Path,
    destination: Path,
    *,
    writable_files: bool = False,
    writable_directories: bool = True,
    materialize_file_links: bool = False,
) -> int: ...
def copy(mount: Path, destination: Path, *, writable_files: bool = False) -> int: ...
