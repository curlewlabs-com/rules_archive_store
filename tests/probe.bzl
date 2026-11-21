"""An action that reads a declared archive through the store, and checks it.

It stands in for a migrated tree's consumer on every worker class, so a change
to the helper or to declared_archive fails here before it reaches one. It says
nothing about any one worker's store: the store's location is outside the
action key, so the result is cached whichever store, if any, first ran it.
"""

load("//archive_store:archives.bzl", "ARCHIVE_MOUNT_ATTRS", "ArchiveInfo", "archive_mounts")

def _archive_probe_impl(ctx):
    mounts = archive_mounts(ctx, "probe", {
        "tree-%d" % index: archive
        for index, archive in enumerate(ctx.attr.archives)
    })
    output = ctx.actions.declare_file(ctx.label.name + ".txt")
    ctx.actions.run(
        executable = mounts.executable,
        arguments = mounts.arguments + [
            "/bin/sh",
            "-c",
            # Runs each archive's own executable and reads its file in place,
            # then requires the expected answer; set -e fails the action on
            # any miss.
            'set -eu; out=$1; shift; for tree; do : >"$out"; "$tree/bin/answer" >>"$out"; ' +
            # A private mount must be on the output volume even if the host's
            # default temporary directory is on a different filesystem.
            'if [ -z "${RULES_ARCHIVE_STORE_DIR:-}" ]; then ' +
            'case "$(cd "$tree" && pwd -P)/" in "$(cd "${out%/*}" && pwd -P)"/archive-store.*/*) ;; *) exit 1 ;; esac; fi; ' +
            'cat "$tree/share/payload.txt" >>"$out"; ' +
            # The archive's dropped directory must not reach the tree.
            'test ! -e "$tree/include"; ' +
            'printf "mounted\\narchive probe payload\\n" | cmp -s - "$out"; done',
            "archive_probe",
            output.path,
        ] + [mounts.paths[mount] for mount in sorted(mounts.paths)],
        inputs = mounts.inputs,
        outputs = [output],
        mnemonic = "ArchiveProbe",
        progress_message = "Reading %{label} through the archive store",
    )
    return [DefaultInfo(files = depset([output]))]

archive_probe = rule(
    implementation = _archive_probe_impl,
    attrs = ARCHIVE_MOUNT_ATTRS | {
        "archives": attr.label_list(providers = [ArchiveInfo], mandatory = True),
    },
)
