"""A rule of this consumer's own that reads archives, and one that reads a tree."""

load(
    "@curlewlabs_rules_archive_store//archive_store:archives.bzl",
    "ARCHIVE_MOUNT_ATTRS",
    "ArchiveInfo",
    "archive_mounts",
    "archive_tree",
    "archive_tree_attrs",
)

_READ = 'set -eu; out=$1; shift; : >"$out"; for tree; do "$tree/bin/hello" >>"$out"; done'

def _read_archive_impl(ctx):
    mounts = archive_mounts(ctx, "read", {"tool": ctx.attr.archive})
    output = ctx.actions.declare_file(ctx.label.name + ".txt")
    ctx.actions.run(
        executable = mounts.executable,
        arguments = mounts.arguments + ["/bin/sh", "-c", _READ, "read", output.path, mounts.paths["tool"]],
        inputs = mounts.inputs,
        outputs = [output],
    )
    return [DefaultInfo(files = depset([output]))]

read_archive = rule(
    implementation = _read_archive_impl,
    attrs = ARCHIVE_MOUNT_ATTRS | {
        "archive": attr.label(providers = [ArchiveInfo], mandatory = True),
    },
)

def _read_tree_impl(ctx):
    tree = archive_tree(ctx, "read", {"first": ctx.attr.archive, "second": ctx.attr.archive}, {})
    output = ctx.actions.declare_file(ctx.label.name + ".txt")
    ctx.actions.run(
        executable = tree.executable,
        arguments = tree.arguments + ["/bin/sh", "-c", _READ, "read", output.path, tree.path + "/first", tree.path + "/second"],
        inputs = tree.inputs,
        tools = tree.tools,
        outputs = [output],
    )
    return [DefaultInfo(files = depset([output]))]

read_tree = rule(
    implementation = _read_tree_impl,
    attrs = archive_tree_attrs(Label("@consumer_bun//:bun")) | {
        "archive": attr.label(providers = [ArchiveInfo], mandatory = True),
    },
)
