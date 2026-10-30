# Archive store rules for [Bazel](https://bazel.build)

Declare a toolchain, SDK or dependency tree as its archive, so an action reads
it as one input instead of thousands of files, and have each remote worker
expand that archive once and reuse the expansion for every later action.

A remote worker lays out every declared input file in an action's build
directory and removes it at teardown, in every action that declares it. A tree
of many small files, such as an Android SDK, a Flutter SDK, a pub cache or an
npm closure, pays that per file in every action that reads it. With these
rules, the tree's archive is the action's input, and the action reads the tree
in place from a store the worker keeps: the first action on a worker instance
expands the archive there, and later ones link to it. A tree of many archives,
such as an npm closure of one archive per package, is published once as a
linked tree and mounted with one link.

This module is at major version zero: a release promises no compatibility with
the one before it. It is not published in the Bazel Central Registry.

## What it changed, as measured

On the macOS Buildbarn workers of the deployment these rules came from, with the
Android SDK, the pub cache, the npm closures and the Flutter SDK declared as
archives, `bb_worker`'s CPU against the CPU of the actions it ran fell from 0.49
to 0.11 on one worker, and was 0.16 on another. Before, about 94% of it was
kernel time, creating and removing input files.

For one npm consumer's tool closure, median worker time per action:

| Representation                             | macOS  | Linux  |
| ------------------------------------------ | ------ | ------ |
| Per-file packages and links                | 14.4 s | 1.4 s  |
| Archives, every package mounted per action | 7.9 s  | 7.6 s  |
| Archives, a published linked tree          | 2.2 s  | 0.6 s  |
| The same, when the tree must first be built | 12.2 s | 20.4 s |

On Linux, whose workers hardlink inputs, per-file was already cheap there, so
that deployment declares npm closures as archives on macOS only, and the large
SDK trees on both.

## Use

Add the module from a release; each
[release](https://github.com/curlewlabs-com/rules_archive_store/releases)
carries the `bazel_dep` and `archive_override` to copy. The conversion runs on
the Python 3.14 toolchain rules_python registers by default.

Declare a tree as its archive:

```starlark
load("@curlewlabs_rules_archive_store//archive_store:archives.bzl", "declared_archive")

declared_archive(
    name = "sdk",
    archive = "@sdk_archive//file",
    sha256 = "<the archive's SHA-256, as its lock pins it>",
    strip_prefix = "sdk-1.2.3",
)
```

`integrity` takes an npm lock's `sha512-` value instead, and `sha256_file` a
digest file a producing action writes. An archive too large for one cache blob
is downloaded as ordered parts by `archive_parts` in `archive_parts.bzl`.

Read it from a test or a `bazel run` binary:

```starlark
load("@curlewlabs_rules_archive_store//archive_store:archives.bzl", "archive_test")

archive_test(
    name = "sdk_test",
    archives = {"sdk": ":sdk"},
    test = ":sdk_check",
)
```

The test finds the tree at `$ARCHIVE_MOUNT_ROOT/sdk`. `archive_binary` does the
same for `bazel run`.

Read it from an action in your own rule:

```starlark
load("@curlewlabs_rules_archive_store//archive_store:archives.bzl", "ARCHIVE_MOUNT_ATTRS", "archive_mounts")

def _impl(ctx):
    mounts = archive_mounts(ctx, "build", {"sdk": ctx.attr.sdk})
    ctx.actions.run(
        executable = mounts.executable,
        arguments = mounts.arguments + [ctx.executable.tool.path, mounts.paths["sdk"]],
        inputs = mounts.inputs,
        tools = [ctx.attr.tool[DefaultInfo].files_to_run],
        outputs = [...],
    )

my_rule = rule(
    implementation = _impl,
    attrs = ARCHIVE_MOUNT_ATTRS | {...},
)
```

A linked tree of many archives (`archive_tree`, and the launchers'
`archive_tree_*` and `tree` forms) is built on macOS only, with `clonefile(2)`,
under a Bun the consumer names, since a tree's consumer runs Bun to use it:

```starlark
load("@curlewlabs_rules_archive_store//archive_store:archives.bzl", "archive_tree", "archive_tree_attrs")

my_tree_rule = rule(
    implementation = _impl,
    attrs = archive_tree_attrs(Label("@my_bun//:bun")) | {...},
)
```

`archive_store/archives.bzl` documents each rule and function, and
[docs/store.md](docs/store.md) is the full mechanism: the conversion and its
member checks, the store's publication protocol, holds, linked trees and
eviction.

## What the executor provides

Without an executor that keeps a store, every action expands its archives into
a private store and removes it on exit: correct, and no faster. Reuse needs the
executor to keep a store per instance, empty it at each start, name it to
actions in `RULES_ARCHIVE_STORE_DIR` outside their key, and run the evictor
beside it. [docs/executor.md](docs/executor.md) states that contract, with
Buildbarn examples in [examples/buildbarn](examples/buildbarn). Buildbarn is the
only executor these rules have run under.

### macOS workers

Upstream `bb_worker` hardlinks inputs from its cache into native build
directories, which on macOS lets concurrent actions sharing a build directory
read each other's files. The store does not go through that path, but the rest
of each action's inputs do: run a worker with the cloning file fetcher proposed
in
[bb-remote-execution#284](https://github.com/buildbarn/bb-remote-execution/pull/284)
([docs/executor.md](docs/executor.md#on-macos-how-the-worker-lays-out-other-inputs)).

### Why not the NFSv4 build directory?

Upstream recommends `bb_worker`'s NFSv4 build directory on macOS, which lays
out no input file on disk. The deployment these rules came from does not run
it: writable copies of inputs would go through the mount, scratch space would
land on it, writes go through the worker's file pool, a worker restart strands
processes using the mount, and simulators, `codesign` and app installs are
untried from it. None of those was measured on its workload
([docs/executor.md](docs/executor.md#why-not-buildbarns-nfsv4-build-directory)).

## Versions

Developed with Bazel 9.3.0 ([.bazelversion](.bazelversion)), rules_python 2.4.2
and its Python 3.14 ([MODULE.bazel](MODULE.bazel)), and Bun 1.3.10 for the
linked tree suite ([tests/deps.bzl](tests/deps.bzl)), on Linux and macOS. These
are the tested selections, not a compatibility range.

## License

MIT; see [LICENSE](LICENSE).
