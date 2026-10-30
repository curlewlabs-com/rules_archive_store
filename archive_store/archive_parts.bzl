"""Download a pinned archive as the ordered parts declared_archive reads.

A remote cache refuses a blob larger than it is configured to hold, and an SDK
archive can outgrow that. A repository
that downloads such an archive splits it here, before it is ever an action
input, and hands declared_archive the parts in order; the conversion reads them
as one stream and checks the lock's digest over the whole archive
(archive_to_tar.py). An archive no larger than PART_BYTES is one part.
"""

# A cache that serves these parts and the converted tar's must accept a blob
# of this size (docs/executor.md); a Buildbarn store whose block is larger
# still holds more than one. declared_archive's conversion writes its tar in
# parts of this size too.
PART_BYTES = 512 << 20

def download_archive_parts(rctx, urls, sha256, name, directory):
    """Download an archive in parts.

    Args:
      rctx: the repository context.
      urls: where the archive is.
      sha256: the whole archive's digest, which the download checks.
      name: the archive's file name, whose suffix says its format.
      directory: the repository directory the parts go in.

    Returns:
      The parts' names in `directory`, `<name>.part-aaa` first, in order.
    """
    whole = directory + "/" + name
    rctx.download(url = urls, output = whole, sha256 = sha256)

    # split names its outputs `aaa`, `aab`, ... on macOS and Linux alike, as
    # archive_parts.py's PART_SUFFIX names a part.
    prefix = name + ".part-"
    result = rctx.execute([
        "/usr/bin/split",
        "-b",
        str(PART_BYTES),
        "-a",
        "3",
        whole,
        directory + "/" + prefix,
    ])
    if result.return_code != 0:
        fail("could not split %s into parts: %s" % (name, result.stderr))
    rctx.delete(whole)
    return sorted([
        entry.basename
        for entry in rctx.path(directory).readdir()
        if entry.basename.startswith(prefix)
    ])

def _archive_parts_impl(rctx):
    parts = download_archive_parts(rctx, rctx.attr.urls, rctx.attr.sha256, rctx.attr.downloaded_file_path, "file")
    rctx.file("file/sha256", rctx.attr.sha256 + "\n", executable = False)
    rctx.file("file/BUILD.bazel", """package(default_visibility = ["//visibility:public"])

# The archive's parts, in order, for declared_archive's `archive`.
filegroup(
    name = "file",
    srcs = {parts},
)

# The whole archive's digest, for declared_archive's `sha256_file`.
exports_files(["sha256"])
""".format(parts = repr(parts)))
    rctx.file("BUILD.bazel", "")

    # The parts are the pinned archive cut by byte count, so Bazel may keep the
    # repository in the host's shared repo contents cache and reuse it across
    # lanes.
    return rctx.repo_metadata(reproducible = True)

archive_parts = repository_rule(
    implementation = _archive_parts_impl,
    doc = "A pinned archive downloaded as ordered parts of at most PART_BYTES: `@<name>//file` and `@<name>//file:sha256`.",
    attrs = {
        "downloaded_file_path": attr.string(
            doc = "The archive's file name, whose suffix says its format.",
            mandatory = True,
        ),
        "sha256": attr.string(doc = "The whole archive's digest, from its lock.", mandatory = True),
        "urls": attr.string_list(mandatory = True),
    },
)
