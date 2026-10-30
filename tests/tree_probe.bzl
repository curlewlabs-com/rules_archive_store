"""Read the probe archives as a linked tree, from an action and from a test.

Rules of a tree's consumer take the tree helper's attributes from
archive_tree_attrs with the consumer's own Bun; these do the same with the
suite's, so a change to that seam, the tree helper or its launchers fails here.
Trees are built on macOS only (archive_tree.ts).
"""

load(
    "//archive_store:archives.bzl",
    "ARCHIVE_MOUNT_ATTRS",
    "ArchiveInfo",
    "archive_test_launcher",
    "archive_tree",
    "archive_tree_attrs",
)

_BUN = Label("@test_bun//:bun")

def _tree(ctx):
    return struct(
        units = {"a": ctx.attr.a, "b": ctx.attr.b},
        # A relative link between units, which the tree resolves in place.
        links = {"link": "a"},
        executables = ["b/bin/answer"],
    )

def _archive_tree_probe_impl(ctx):
    tree = _tree(ctx)
    read = archive_tree(ctx, "probe", tree.units, tree.links, executables = tree.executables)
    output = ctx.actions.declare_file(ctx.label.name + ".txt")
    ctx.actions.run(
        executable = read.executable,
        arguments = read.arguments + [
            "/bin/sh",
            "-c",
            # Each unit's own executable, a file read through the link, and the
            # dropped directory absent; set -e fails the action on any miss.
            'set -eu; out=$1; tree=$2; : >"$out"; ' +
            'for unit in a b; do "$tree/$unit/bin/answer" >>"$out"; done; ' +
            'cat "$tree/link/share/payload.txt" >>"$out"; ' +
            'test ! -e "$tree/a/include"; ' +
            'printf "mounted\\nmounted\\narchive probe payload\\n" | cmp -s - "$out"',
            "archive_tree_probe",
            output.path,
            read.path,
        ],
        inputs = read.inputs,
        tools = read.tools,
        outputs = [output],
        mnemonic = "ArchiveTreeProbe",
        progress_message = "Reading %{label} as a linked tree",
    )
    return [DefaultInfo(files = depset([output]))]

archive_tree_probe = rule(
    implementation = _archive_tree_probe_impl,
    attrs = archive_tree_attrs(_BUN) | {
        "a": attr.label(providers = [ArchiveInfo], mandatory = True),
        "b": attr.label(providers = [ArchiveInfo], mandatory = True),
    },
)

def _archive_tree_probe_test_impl(ctx):
    launch = archive_test_launcher(ctx, ctx.attr.archives, ctx.attr.test, tree = _tree(ctx))
    return [
        DefaultInfo(executable = launch.executable, runfiles = launch.runfiles),
        RunEnvironmentInfo(environment = launch.environment),
    ]

archive_tree_probe_test = rule(
    implementation = _archive_tree_probe_test_impl,
    attrs = ARCHIVE_MOUNT_ATTRS | archive_tree_attrs(_BUN, launched_bun = _BUN) | {
        "a": attr.label(providers = [ArchiveInfo], mandatory = True),
        "archives": attr.string_keyed_label_dict(providers = [ArchiveInfo]),
        "b": attr.label(providers = [ArchiveInfo], mandatory = True),
        "test": attr.label(executable = True, cfg = "target", mandatory = True),
    },
    test = True,
)
