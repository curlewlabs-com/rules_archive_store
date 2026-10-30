"""Declare a tree as its archive, and mount archives into an action's input root.

A toolchain, SDK or dependency tree the rule weighs as an archive is one input,
its archive, not its files (docs/store.md). `declared_archive` names an archive and its
digest; an action that reads the tree runs its command through
`archive_mount.sh`, which `archive_mounts` prepares. An action that reads a
linked tree of many archives runs it through `archive_tree.ts`, which
`archive_tree` prepares (docs/store.md sec."Linked trees").
"""

load(":archive_parts.bzl", "PART_BYTES")

def _archive_info_init(**_fields):
    # The helper extracts only tars this file's conversion wrote and checked,
    # so no other rule may stand a tar of its own in for one.
    fail("ArchiveInfo is created only by declared_archive, which converts and checks the archive")

ArchiveInfo, _new_archive_info = provider(
    doc = "One archive standing for a tree, and the digest the store keys it by. Only declared_archive creates one.",
    init = _archive_info_init,
    fields = {
        "archive": "File: the directory of the parts of the tar the store expands, which declared_archive's conversion wrote; joined in name order, they are the tar.",
        "verified_digest": "File: that tar's SHA-256, written only by the conversion, after it found the declared archive's digest.",
        "strip_prefix": "str: the directory inside the archive that is the tree's root, or '' for the archive's root.",
    },
)

# The helper runs under the system shell rather than as an executable input,
# since the remote input tree does not preserve a source file's mode.
_HELPER = Label("//archive_store:archive_mount.sh")

def _manifest_field(value, what):
    if "|" in value or "\n" in value:
        fail("%s cannot hold '|' or a newline: %s" % (what, value))
    return value

def _declared_archive_impl(ctx):
    if len([declared for declared in [ctx.attr.sha256, ctx.file.sha256_file, ctx.attr.integrity] if declared]) != 1:
        fail("declare exactly one of sha256, sha256_file and integrity")
    if ctx.attr.strip_components < 0:
        fail("strip_components cannot be negative")
    _manifest_field(ctx.attr.strip_prefix, "strip_prefix")

    # The store reuses an expansion by digest without hashing the archive
    # again, and extracts only a tar this conversion wrote, so this action is
    # what ties the digest to the bytes the store expands and decides what the
    # tree may hold: it checks the archive against its declared digest and
    # every kept member's path and link, after leaving out what `drop` names
    # (archive_to_tar.py says which checks a dropped member still gets), once
    # per archive version under Bazel's cache key, and every consumer reads the
    # digest of the tar it wrote.
    declared = ctx.attr.sha256 or ctx.attr.integrity or "@" + ctx.file.sha256_file.path

    # In name order, which is a part's place in the stream: a target's files
    # come in an order Bazel does not promise. The conversion still refuses a
    # gap or a name that is not a part's.
    parts = sorted(ctx.files.archive, key = lambda part: part.basename)
    declared_inputs = parts + ([ctx.file.sha256_file] if ctx.file.sha256_file else [])
    verified = ctx.actions.declare_file(ctx.label.name + ".verified.sha256")

    # A larger part could pass the largest blob a store accepts, which is what
    # PART_BYTES stays under.
    if ctx.attr.part_bytes < 1 or ctx.attr.part_bytes > PART_BYTES:
        fail("part_bytes must be from 1 to PART_BYTES (%d), not %d" % (PART_BYTES, ctx.attr.part_bytes))

    # A directory, since the conversion alone learns how many parts the tar
    # takes.
    archive = ctx.actions.declare_directory(ctx.label.name + ".converted.parts")
    arguments = [
        argument
        for part in parts
        for argument in ["--archive", part.path]
    ] + [
        "--digest",
        declared,
        "--tar-parts",
        archive.path,
        "--part-bytes",
        str(ctx.attr.part_bytes),
        "--tar-digest",
        verified.path,
        "--strip-components",
        str(ctx.attr.strip_components),
    ]
    outputs = [archive, verified]
    if bool(ctx.attr.drop) != bool(ctx.attr.dropped_sha256):
        fail("declare drop and dropped_sha256 together")
    if ctx.attr.drop:
        # Built beside the tar, so a reviewer of the archive reads what it
        # left out without opening it.
        dropped = ctx.actions.declare_file(ctx.label.name + ".dropped.txt")
        outputs.append(dropped)
        arguments.extend(["--dropped", dropped.path, "--dropped-digest", ctx.attr.dropped_sha256])
        for pattern in ctx.attr.drop:
            arguments.extend(["--drop", pattern])
    ctx.actions.run(
        executable = ctx.executable._archive_to_tar,
        arguments = arguments,
        inputs = declared_inputs,
        outputs = outputs,
        mnemonic = "ArchiveToTar",
        progress_message = "Converting %{label}'s archive to the tar the archive store reads",
    )
    return [
        DefaultInfo(files = depset(outputs)),
        _new_archive_info(
            archive = archive,
            verified_digest = verified,
            strip_prefix = ctx.attr.strip_prefix,
        ),
    ]

declared_archive = rule(
    implementation = _declared_archive_impl,
    doc = "A tree declared as its archive, a tar or a zip. A pinned archive carries its lock's digest; a built one, its producer's digest file.",
    attrs = {
        "archive": attr.label(
            allow_files = True,
            mandatory = True,
            doc = "The archive: one file, or its parts, which the conversion reads in name order, as archive_parts.bzl downloads one too large for a store blob. The digest is the whole archive's.",
        ),
        "sha256": attr.string(),
        "sha256_file": attr.label(allow_single_file = True),
        "integrity": attr.string(
            doc = "The archive's SHA-512 as a Subresource Integrity value, `sha512-<base64>`, the form an npm lock pins.",
        ),
        "strip_prefix": attr.string(),
        "strip_components": attr.int(
            doc = "Leading components the conversion strips from each tar member's name, as npm strips a package tarball's top directory whatever it is named. The tree's root is then what is left.",
        ),
        "drop": attr.string_list(
            doc = "Members to leave out of the tree, each a pattern over member paths from the archive's root, matched a component at a time, with everything beneath a match. Only for members that cannot exist on a case-insensitive volume and that nothing built reads; a pattern that matches nothing fails the conversion.",
        ),
        "dropped_sha256": attr.string(
            doc = "The SHA-256 of the list of members `drop` leaves out, one per line in the archive's order, as `<name>.dropped.txt` holds it. A drop that leaves out anything else fails, printing the list it made.",
        ),
        "part_bytes": attr.int(
            default = PART_BYTES,
            doc = "The largest part of the converted tar, at most PART_BYTES. Only the archive probe sets it, smaller, so that its tar is several parts on every worker class.",
        ),
        "_archive_to_tar": attr.label(
            default = Label("//archive_store:archive_to_tar"),
            executable = True,
            cfg = "exec",
        ),
    },
    provides = [ArchiveInfo],
)

ARCHIVE_MOUNT_ATTRS = {
    "_archive_mount": attr.label(default = _HELPER, allow_single_file = True),
}

def archive_mounts(ctx, name, mounts):
    """Prepare an action that reads `mounts` through the archive store.

    Args:
      ctx: the rule context; the rule's attrs include ARCHIVE_MOUNT_ATTRS.
      name: distinguishes this action's manifest and mount root among the
        target's actions.
      mounts: dict from a mount name to the target mounted there, which
        provides ArchiveInfo; a target, not a provider, so only what
        declared_archive converted and checked can reach the helper. The
        helper refuses a name that is not a normalized relative path, or one
        that sits inside another.

    Returns:
      A struct whose `executable` is the action's executable, with
      `arguments` ahead of the command's own, and whose `inputs` the action
      declares. `paths` maps each mount name to the execution-root path the
      command reads the tree at. The mount root sits beside the manifest, so
      each call has its own, and an action that alone runs the result has its
      own even in an unsandboxed execution root. Where several actions run the
      result, as a toolchain's launchers are run (docs/store.md),
      they share the root, and each needs an input root of its own.
    """
    manifest = ctx.actions.declare_file(ctx.label.name + "." + name + ".archives")
    root = manifest.path + ".d"
    lines = []
    helper = ctx.file._archive_mount
    inputs = [helper, manifest]
    paths = {}
    for mount, target in mounts.items():
        if type(target) != "Target" or ArchiveInfo not in target:
            fail("archive mount %s must be a declared_archive target, not %s" % (mount, type(target)))
        info = target[ArchiveInfo]
        path = root + "/" + mount

        # Every emitted field, generated paths included: a target name may
        # hold `|`, which would shift the fields after it.
        lines.append("|".join([
            _manifest_field(path, "archive mount path"),
            _manifest_field("@" + info.verified_digest.path, "digest path"),
            _manifest_field(info.archive.path, "archive path"),
            _manifest_field(info.strip_prefix or ".", "strip_prefix"),
        ]))
        inputs.extend([info.archive, info.verified_digest])
        paths[mount] = path
    ctx.actions.write(manifest, "\n".join(lines) + "\n")
    return struct(
        executable = "/bin/sh",
        arguments = [helper.path, manifest.path, "--"],
        inputs = inputs,
        paths = paths,
    )

_TREE_HELPER = Label("//archive_store:archive_tree.ts")
_TREE_MANIFEST = Label("//archive_store:archive_tree_manifest.ts")

def archive_tree_attrs(bun, launched_bun = None):
    """The attributes a rule that reads a linked tree adds to its own.

    The linked tree helper runs under Bun, which a tree's consumer already
    carries to run its bins, so the consumer names it; this module depends on
    no Bun of its own.

    Args:
      bun: the Bun that resolves a tree's manifest and runs the helper in an
        action, for the execution platform: an executable target, or the `bun`
        file of a downloaded release.
      launched_bun: for a rule that launches a test or a `bazel run` binary
        with a tree (archive_tree_test_launcher, archive_tree_run_launcher, or
        archive_test_launcher's `tree`), the Bun that runs the helper at run
        time, built for the target platform. It may be the same label as
        `bun`, unless the rule also names that Bun in its `args`, whose
        expansion refuses a label held twice; alias one of them then.

    Returns:
      A dict of attributes to merge into the rule's own.
    """
    attrs = {
        "_archive_tree": attr.label(default = _TREE_HELPER, allow_single_file = True),
        "_archive_tree_manifest": attr.label(default = _TREE_MANIFEST, allow_single_file = True),
        "_archive_tree_bun": attr.label(default = bun, executable = True, allow_files = True, cfg = "exec"),
    }
    if launched_bun != None:
        # A launched test runs the helper under the target configuration's
        # Bun, and its manifest is still resolved by an action, under the
        # exec one.
        attrs["_archive_tree_test_bun"] = attr.label(default = launched_bun, executable = True, allow_files = True, cfg = "target")
    return attrs

def _tree_manifest(ctx, manifest, mount, units, links, executables, path):
    """Write the archive_tree.ts manifest of a tree; the archives it reads.

    The digests are resolved by an action of their own
    (archive_tree_manifest.ts --resolve), so the reader takes neither a digest file per unit nor
    the time to read each. `path` maps an archive to how the helper reads it:
    by exec path in an action, by runfiles path in a launched test.
    """
    template = ctx.actions.declare_file(manifest.basename + ".in", sibling = manifest)
    lines = ["mount|" + _manifest_field(mount, "tree mount path")]
    archives = []
    digests = []
    for dest, target in sorted(units.items()):
        if type(target) != "Target" or ArchiveInfo not in target:
            fail("tree unit %s must be a declared_archive target, not %s" % (dest, type(target)))
        info = target[ArchiveInfo]
        lines.append("|".join([
            "unit",
            _manifest_field(dest, "tree unit path"),
            _manifest_field("@" + info.verified_digest.path, "digest path"),
            _manifest_field(path(info.archive), "archive path"),
            _manifest_field(info.strip_prefix or ".", "strip_prefix"),
        ]))
        archives.append(info.archive)
        digests.append(info.verified_digest)
    for at, target in sorted(links.items()):
        lines.append("|".join(["link", _manifest_field(at, "tree link path"), _manifest_field(target, "tree link target")]))
    for file in sorted(executables):
        lines.append("exec|" + _manifest_field(file, "tree executable path"))
    ctx.actions.write(template, "\n".join(lines) + "\n")
    bun = ctx.attr._archive_tree_bun[DefaultInfo].files_to_run
    ctx.actions.run(
        executable = bun,
        arguments = ["--no-install", ctx.file._archive_tree_manifest.path, "--resolve", template.path, manifest.path],
        inputs = [ctx.file._archive_tree_manifest, template] + digests,
        outputs = [manifest],
        mnemonic = "ArchiveTreeManifest",
        progress_message = "Writing %{label}'s linked tree manifest",
    )
    return archives

def archive_tree(ctx, name, units, links, writable = False, executables = []):
    """Prepare an action that reads a linked tree of `units` through the archive store.

    The worker instance's store publishes the tree once, keyed by what it
    holds, and the action mounts it with one link (archive_tree.ts,
    docs/store.md sec."Linked trees").

    Args:
      ctx: the rule context; the rule's attrs include archive_tree_attrs'.
      name: distinguishes this action's manifest and mount among the
        target's actions.
      units: dict from a path in the tree to the declared_archive target
        whose expansion is cloned there, a real directory; several paths may
        name one target.
      links: dict from a path in the tree to the relative target of the
        symlink made there. The helper refuses one that leaves the tree.
      writable: mount a writable clone of the whole tree rather than a link
        to the published one, for a command that writes into its tree.
      executables: paths in the tree, each a regular file inside a unit's
        clone, that the tree marks executable whatever mode its archive gave
        them, such as a package's bins.

    Returns:
      A struct whose `executable` is the action's executable, with
      `arguments` ahead of the command's own, whose `inputs` and `tools` the
      action declares, and whose `path` is the execution-root path the
      command reads the tree at.
    """
    manifest = ctx.actions.declare_file(ctx.label.name + "." + name + ".tree")
    mount = manifest.path + ".d/tree"
    archives = _tree_manifest(ctx, manifest, mount, units, links, executables, lambda file: file.path)
    bun = ctx.attr._archive_tree_bun[DefaultInfo].files_to_run
    helper = ctx.file._archive_tree
    return struct(
        executable = bun,
        arguments = ["--no-install", helper.path] + (["--writable"] if writable else []) + [manifest.path, "--"],
        inputs = depset([helper, ctx.file._archive_tree_manifest, manifest] + archives),
        tools = [bun],
        path = mount,
    )

def archive_tree_test_launcher(ctx, units, links, test, data = [], env = {}, executables = []):
    """Launches `test` with a linked tree mounted at `$ARCHIVE_MOUNT_ROOT/tree`.

    Args:
      ctx: the rule context; the rule's attrs include archive_tree_attrs' with a launched_bun,
        and the rule is a test.
      units: as archive_tree takes them.
      links: as archive_tree takes them.
      executables: as archive_tree takes them.
      test: the executable target that runs as the test.
      data: targets the test reads at run time, which the rule's `args` and
        `env` name.
      env: the test's environment, with `$(rootpath)` of `data` unexpanded.

    Returns:
      A struct whose `executable` is the test's launcher, whose `runfiles` the
      test carries, and whose `environment` is `env` expanded.
    """
    executable = test[DefaultInfo].files_to_run.executable
    manifest = ctx.actions.declare_file(ctx.label.name + ".tree")
    archives = _tree_manifest(ctx, manifest, "tree", units, links, executables, lambda file: file.short_path)
    bun = ctx.attr._archive_tree_test_bun[DefaultInfo].files_to_run.executable
    helper = ctx.file._archive_tree
    for path in [bun.short_path, helper.short_path, manifest.short_path, executable.short_path]:
        # The launcher quotes each path in single quotes.
        if "'" in path:
            fail("an archive tree launcher cannot launch a path holding a single quote: " + path)

    # The tree is mounted under the test's own temporary directory, which the
    # helper names to the test in ARCHIVE_MOUNT_ROOT, and the helper execs the
    # test, so its holds last exactly as long as the test does.
    script = "#!/bin/sh\nARCHIVE_TEST_STARTED=$(date +%s)\nexport ARCHIVE_TEST_STARTED\nexec '{bun}' --no-install '{helper}' --root \"${{TEST_TMPDIR:?}}/archive-trees\" '{manifest}' -- '{test}' \"$@\"\n".format(
        bun = bun.short_path,
        helper = helper.short_path,
        manifest = manifest.short_path,
        test = executable.short_path,
    )
    launched = struct(files = [helper, ctx.file._archive_tree_manifest, manifest] + archives)
    return _launch(ctx, launched, test, script, [ctx.attr._archive_tree_test_bun] + data, env)

def archive_tree_run_launcher(ctx, units, links, binary, data = [], env = {}, executables = []):
    """Launches `binary` under `bazel run` with a linked tree mounted at `$ARCHIVE_MOUNT_ROOT/tree`.

    Args:
      ctx: the rule context; the rule's attrs include archive_tree_attrs' with a launched_bun,
        and the rule is executable.
      units: as archive_tree takes them.
      links: as archive_tree takes them.
      executables: as archive_tree takes them.
      binary: the executable target that runs, with the tree in place.
      data: targets the binary reads at run time, which the rule's `args` and
        `env` name.
      env: the binary's environment, with `$(rootpath)` of `data` unexpanded.

    Returns:
      A struct whose `executable` is the binary's launcher, whose `runfiles`
      the rule carries, and whose `environment` is `env` expanded.
    """
    executable = binary[DefaultInfo].files_to_run.executable
    manifest = ctx.actions.declare_file(ctx.label.name + ".tree")
    archives = _tree_manifest(ctx, manifest, "tree", units, links, executables, lambda file: file.short_path)
    bun = ctx.attr._archive_tree_test_bun[DefaultInfo].files_to_run.executable
    helper = ctx.file._archive_tree
    for path in [bun.short_path, helper.short_path, manifest.short_path, executable.short_path]:
        # The launcher quotes each path in single quotes.
        if "'" in path:
            fail("an archive tree launcher cannot launch a path holding a single quote: " + path)

    # As archive_run_launcher's: the launcher makes the mount root `bazel run`
    # does not give and removes it once the helper returns. The root holds only
    # the tree's mount link, so removing it never reaches the store, and with a
    # private store the helper removes that store when the binary exits. Bun's
    # wrapper finds its runfiles from a $0 that holds a directory, so it is
    # named from $PWD, the runfiles' `_main`.
    script = """#!/bin/sh
set -eu
if [ -n "${{RUNNER_TEMP:-}}" ]; then
    TMPDIR=$RUNNER_TEMP
    export TMPDIR
fi
root=$(mktemp -d "${{TMPDIR:-/tmp}}/archive-trees.XXXXXX")
trap '/bin/rm -rf "$root"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
"$PWD/"'{bun}' --no-install '{helper}' --root "$root" '{manifest}' -- '{binary}' "$@" && status=0 || status=$?
exit "$status"
""".format(
        bun = bun.short_path,
        helper = helper.short_path,
        manifest = manifest.short_path,
        binary = executable.short_path,
    )
    launched = struct(files = [helper, ctx.file._archive_tree_manifest, manifest] + archives)
    return _launch(ctx, launched, binary, script, [ctx.attr._archive_tree_test_bun] + data, env)

def _runfiles_manifest(ctx, mounts, executable):
    """The manifest naming `mounts` by runfiles path, with the files it reads.

    A launched executable runs from its runfiles tree, so the manifest names the
    archives and digests by runfiles path, which the helper reads from there.
    """
    manifest = ctx.actions.declare_file(ctx.label.name + ".archives")
    helper = ctx.file._archive_mount
    files = [helper, manifest]
    lines = []
    for mount, target in mounts.items():
        if type(target) != "Target" or ArchiveInfo not in target:
            fail("archive mount %s must be a declared_archive target, not %s" % (mount, type(target)))
        info = target[ArchiveInfo]
        lines.append("|".join([
            _manifest_field(mount, "archive mount path"),
            _manifest_field("@" + info.verified_digest.short_path, "digest path"),
            _manifest_field(info.archive.short_path, "archive path"),
            _manifest_field(info.strip_prefix or ".", "strip_prefix"),
        ]))
        files.extend([info.archive, info.verified_digest])
    ctx.actions.write(manifest, "\n".join(lines) + "\n")
    for path in [helper.short_path, manifest.short_path, executable.short_path]:
        # The launcher quotes each path in single quotes.
        if "'" in path:
            fail("an archive launcher cannot launch a path holding a single quote: " + path)
    return struct(helper = helper, manifest = manifest, files = files)

def _launch(ctx, launched, executable, script, data, env):
    # Not `<name>.sh`: a shell suite named for its script, `<name>.sh` in the
    # same package, would share that runfiles path, and its runfiles would
    # carry this launcher where the script should be.
    launcher = ctx.actions.declare_file(ctx.label.name + ".archive_launcher.sh")
    ctx.actions.write(launcher, script, is_executable = True)
    runfiles = ctx.runfiles(files = launched.files + [executable.files_to_run.executable]).merge(executable[DefaultInfo].default_runfiles)
    for target in data:
        runfiles = runfiles.merge(ctx.runfiles(transitive_files = target[DefaultInfo].files)).merge(target[DefaultInfo].default_runfiles)
    return struct(
        executable = launcher,
        runfiles = runfiles,
        environment = {name: ctx.expand_location(value, data) for name, value in env.items()},
    )

def archive_test_launcher(ctx, mounts, test, data = [], env = {}, tree = None):
    """Launches `test` with each of `mounts` mounted under ARCHIVE_MOUNT_ROOT.

    Args:
      ctx: the rule context; the rule's attrs include ARCHIVE_MOUNT_ATTRS, and
        the rule is a test; with `tree`, archive_tree_attrs' with a launched_bun too.
      mounts: dict from a mount name to the declared_archive target mounted
        there, as archive_mounts takes it.
      test: the executable target that runs as the test.
      data: targets the test reads at run time, which the rule's `args` and
        `env` name.
      env: the test's environment, with `$(rootpath)` of `data` unexpanded.
      tree: optionally, a linked tree to put at `$ARCHIVE_MOUNT_ROOT/tree`
        beside the mounts: a struct of `units`, `links` and `executables`, as
        archive_tree takes them, which the archive store's helper mounts; or a
        struct of `directory`, a runfiles path the launcher links there, as a
        Linux tree's links and units are the test's runfiles.

    Returns:
      A struct whose `executable` is the test's launcher, whose `runfiles` the
      test carries, and whose `environment` is `env` expanded.
    """
    executable = test[DefaultInfo].files_to_run.executable
    if tree != None:
        for mount in mounts:
            # archive_mount.sh compares mounts case-folded and by path prefix,
            # and the tree helper may write a marker beside its mount.
            folded = mount.lower()
            if folded in ("tree", "tree.writable") or folded.startswith(("tree/", "tree.writable/")):
                fail("archive mount %s collides with the linked tree the launcher puts at tree; name the archive's mount otherwise" % mount)
    launched = _runfiles_manifest(ctx, mounts, executable)

    # The tree goes under the mounts' root, so the helper that mounts it runs
    # as the mount helper's command and the test as its own.
    then = ""
    if tree != None and hasattr(tree, "directory"):
        if "'" in tree.directory:
            fail("an archive launcher cannot link a path holding a single quote: " + tree.directory)
        then = """/bin/sh -c 'ln -s "$PWD/$1" "$ARCHIVE_MOUNT_ROOT/tree" && shift && exec "$@"' sh '{directory}' """.format(directory = tree.directory)
    elif tree != None:
        manifest = ctx.actions.declare_file(ctx.label.name + ".tree")
        archives = _tree_manifest(ctx, manifest, "tree", tree.units, tree.links, tree.executables, lambda file: file.short_path)
        bun = ctx.attr._archive_tree_test_bun[DefaultInfo].files_to_run.executable
        helper = ctx.file._archive_tree
        for path in [bun.short_path, helper.short_path, manifest.short_path]:
            if "'" in path:
                fail("an archive tree launcher cannot launch a path holding a single quote: " + path)

        # Bun's wrapper finds its runfiles from a $0 that holds a directory.
        # The root is the mount helper's own: this launcher expands it before
        # that helper sets ARCHIVE_MOUNT_ROOT.
        then = """"$PWD/"'{bun}' --no-install '{helper}' --root "${{TEST_TMPDIR:?}}/archive-mounts" '{manifest}' -- """.format(
            bun = bun.short_path,
            helper = helper.short_path,
            manifest = manifest.short_path,
        )
        launched = struct(
            helper = launched.helper,
            manifest = launched.manifest,
            files = launched.files + [helper, ctx.file._archive_tree_manifest, manifest] + archives,
        )
        data = data + [ctx.attr._archive_tree_test_bun]

    # The mounts go under the test's own temporary directory, which the helper
    # names to the test in ARCHIVE_MOUNT_ROOT, and the helper execs the test,
    # so its holds last exactly as long as the test does.
    # ARCHIVE_TEST_STARTED: mounting counts against the test's timeout, so a
    # test that keeps its own deadline starts it here, not at its own start.
    script = "#!/bin/sh\nARCHIVE_TEST_STARTED=$(date +%s)\nexport ARCHIVE_TEST_STARTED\nexec /bin/sh '{helper}' --root \"${{TEST_TMPDIR:?}}/archive-mounts\" '{manifest}' -- {then}'{test}' \"$@\"\n".format(
        helper = launched.helper.short_path,
        manifest = launched.manifest.short_path,
        then = then,
        test = executable.short_path,
    )
    return _launch(ctx, launched, test, script, data, env)

def archive_run_launcher(ctx, mounts, binary, data = [], env = {}):
    """Launches `binary` under `bazel run` with each of `mounts` mounted under ARCHIVE_MOUNT_ROOT.

    Args:
      ctx: the rule context; the rule's attrs include ARCHIVE_MOUNT_ATTRS, and
        the rule is executable.
      mounts: dict from a mount name to the declared_archive target mounted
        there, as archive_mounts takes it.
      binary: the executable target that runs, with the mounts in place.
      data: targets the binary reads at run time, which the rule's `args` and
        `env` name.
      env: the binary's environment, with `$(rootpath)` of `data` unexpanded.

    Returns:
      A struct whose `executable` is the binary's launcher, whose `runfiles`
      the rule carries, and whose `environment` is `env` expanded.
    """
    executable = binary[DefaultInfo].files_to_run.executable
    launched = _runfiles_manifest(ctx, mounts, executable)

    # `bazel run` gives no TEST_TMPDIR, so the launcher makes the mount root and
    # removes it once the helper returns, which is why the helper runs as its
    # child. With a shared store the helper execs the binary, so its holds name
    # the live process; with a private one, as outside a worker, the helper
    # stays the binary's parent and removes that store when the binary exits.
    # The root holds only mount links, so removing it never reaches the store.
    # In an Actions job the private store goes under the job's own RUNNER_TEMP:
    # a sweep that ages /tmp by mtime alone, as systemd-tmpfiles does, would
    # delete an expansion during the run, since it keeps its archive's zero
    # mtimes.
    script = """#!/bin/sh
set -eu
if [ -n "${{RUNNER_TEMP:-}}" ]; then
    TMPDIR=$RUNNER_TEMP
    export TMPDIR
fi
root=$(mktemp -d "${{TMPDIR:-/tmp}}/archive-mounts.XXXXXX")
trap '/bin/rm -rf "$root"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
/bin/sh '{helper}' --root "$root" '{manifest}' -- '{binary}' "$@" && status=0 || status=$?
exit "$status"
""".format(
        helper = launched.helper.short_path,
        manifest = launched.manifest.short_path,
        binary = executable.short_path,
    )
    return _launch(ctx, launched, binary, script, data, env)

def _archive_test_impl(ctx):
    launch = archive_test_launcher(ctx, ctx.attr.archives, ctx.attr.test, ctx.attr.data, ctx.attr.env)
    return [
        DefaultInfo(executable = launch.executable, runfiles = launch.runfiles),
        RunEnvironmentInfo(environment = launch.environment),
    ]

archive_test = rule(
    implementation = _archive_test_impl,
    doc = "Runs `test` with each of `archives` mounted at its name under ARCHIVE_MOUNT_ROOT.",
    attrs = {
        "archives": attr.string_keyed_label_dict(
            providers = [ArchiveInfo],
            mandatory = True,
            doc = "Mount name to the declared_archive mounted there.",
        ),
        "data": attr.label_list(allow_files = True, doc = "What `args` and `env` name."),
        "env": attr.string_dict(doc = "Environment for the test, with `$(rootpath)` of `data` expanded."),
        "test": attr.label(
            executable = True,
            cfg = "target",
            mandatory = True,
            doc = "The executable that runs as the test, with the mounts in place.",
        ),
        "_archive_mount": attr.label(default = _HELPER, allow_single_file = True),
    },
    test = True,
)

def _archive_binary_impl(ctx):
    launch = archive_run_launcher(ctx, ctx.attr.archives, ctx.attr.binary, ctx.attr.data, ctx.attr.env)
    return [
        DefaultInfo(executable = launch.executable, runfiles = launch.runfiles),
        RunEnvironmentInfo(environment = launch.environment),
    ]

archive_binary = rule(
    implementation = _archive_binary_impl,
    doc = "Runs `binary` under `bazel run` with each of `archives` mounted at its name under ARCHIVE_MOUNT_ROOT.",
    attrs = {
        "archives": attr.string_keyed_label_dict(
            providers = [ArchiveInfo],
            mandatory = True,
            doc = "Mount name to the declared_archive mounted there.",
        ),
        "binary": attr.label(
            executable = True,
            cfg = "target",
            mandatory = True,
            doc = "The executable that runs, with the mounts in place.",
        ),
        "data": attr.label_list(allow_files = True, doc = "What `args` and `env` name."),
        "env": attr.string_dict(doc = "Environment for the binary, with `$(rootpath)` of `data` expanded."),
        "_archive_mount": attr.label(default = _HELPER, allow_single_file = True),
    },
    executable = True,
)
