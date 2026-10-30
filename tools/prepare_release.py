"""Prepare a source-only release candidate, verify it, and print release notes.

    python3 tools/prepare_release.py [vMAJOR.MINOR.PATCH] > notes.md

The archive is `git archive` of HEAD with ownership, times and gzip metadata
normalized, so its bytes depend on the commit alone. It goes in dist/ with its
SHA256SUMS and a source.json naming the commit; the independent consumer then
builds against it. Logs go to stderr and the notes to stdout.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

MODULE = "curlewlabs_rules_archive_store"
REPOSITORY = "curlewlabs-com/rules_archive_store"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", nargs="?", help="vMAJOR.MINOR.PATCH to verify")
    parser.add_argument("--bazel", default="bazel")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    # The archive is HEAD, so uncommitted changes would be in neither it nor
    # the evidence; Bazel's own lockfile refresh is the exception.
    subprocess.run(
        [
            "git",
            "diff",
            "--exit-code",
            "HEAD",
            "--",
            ".",
            ":(exclude)MODULE.bazel.lock",
        ],
        cwd=root,
        check=True,
        stdout=sys.stderr,
    )
    graph = json.loads(
        subprocess.check_output(
            [
                args.bazel,
                "--ignore_all_rc_files",
                "mod",
                "graph",
                "--output=json",
                "--depth=1",
                "--ignore_dev_dependency",
                "--lockfile_mode=off",
            ],
            cwd=root,
            text=True,
        )
    )
    version = graph["version"]
    tag = args.tag or f"v{version}"
    if (
        graph["name"] != MODULE
        or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag)
        or tag != f"v{version}"
    ):
        print(
            f"release tag {tag!r} does not match the module {graph['name']}@{version}",
            file=sys.stderr,
        )
        return 1
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    prefix = f"rules_archive_store-{version}"
    output = root / "dist"
    output.mkdir(exist_ok=True)
    archive = output / f"{prefix}.tar.gz"
    source = subprocess.check_output(
        ["git", "archive", "--format=tar", f"--prefix={prefix}/", "HEAD"], cwd=root
    )
    with (
        tarfile.open(fileobj=io.BytesIO(source)) as committed,
        archive.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as packaged,
    ):
        for member in committed.getmembers():
            member.mtime = 0
            member.uid = member.gid = 0
            member.uname = member.gname = ""
            member.pax_headers = {}
            packaged.addfile(
                member, committed.extractfile(member) if member.isfile() else None
            )
    digest = hashlib.sha256(archive.read_bytes()).digest()
    integrity = "sha256-" + base64.b64encode(digest).decode()
    (output / "SHA256SUMS").write_text(f"{digest.hex()}  {archive.name}\n")
    (output / "source.json").write_text(
        json.dumps(
            {
                "module": MODULE,
                "version": version,
                "commit": revision,
                "archive": archive.name,
                "integrity": integrity,
                "strip_prefix": prefix,
            },
            indent=2,
        )
        + "\n"
    )
    print(
        f"prepared {archive.name}: {archive.stat().st_size} bytes, {integrity}",
        file=sys.stderr,
        flush=True,
    )
    subprocess.run(
        [
            sys.executable,
            str(root / "tools" / "check_consumer.py"),
            "--bazel",
            args.bazel,
            "--archive",
            str(archive),
            "--strip-prefix",
            prefix,
        ],
        cwd=root,
        check=True,
        stdout=sys.stderr,
    )
    print(f"""Source commit: `{revision}`

Add the verified source archive with Bzlmod:

```starlark
bazel_dep(name = "{MODULE}", version = "{version}")
archive_override(
    module_name = "{MODULE}",
    urls = ["https://github.com/{REPOSITORY}/releases/download/{tag}/{archive.name}"],
    integrity = "{integrity}",
    strip_prefix = "{prefix}",
)
```

Reuse across actions needs an executor that keeps a store
([docs/executor.md](https://github.com/{REPOSITORY}/blob/{tag}/docs/executor.md)).
This release is not a Bazel Central Registry entry.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
