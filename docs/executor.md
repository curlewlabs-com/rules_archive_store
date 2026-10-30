# What an executor provides

The rules work under any Bazel execution: an action that reads an archive
expands it into a private store and removes that store when it exits. Reuse
across actions, the reason for the store, needs the executor that runs them to
keep a store for each of its instances. This is what that executor has to
provide, as [store.md](store.md) relies on it.

Buildbarn's `bb_worker` with native build directories is the only executor these
rules have run under, on Linux and on macOS. Any other is untested; nothing here
is specific to Buildbarn beyond the examples.

## The contract

- **A store on the build directory's volume.** A directory beside the build
  directory, on the same volume, so a linked tree can be cloned into an
  action's directory; a clone across volumes fails.
- **Writable by every action account.** The actions themselves expand archives
  into it, so every account an action runs as writes it: on Buildbarn, every
  runner's account, through a group they share, with the directory set-group-ID
  on Linux so new entries keep that group. No cached action can check this, so
  check it when the executor is deployed, once the store exists.
- **Emptied at each instance start.** Nothing flushes an expansion to disk
  before publishing it, so after a crash a published expansion can be
  truncated; the start discards it. Setting the old store aside by rename and
  removing it in the background keeps a full store from holding the start.
- **Named to actions outside their key.** `RULES_ARCHIVE_STORE_DIR` holds the
  store's absolute path in every action's environment, set by the executor's
  own configuration rather than by the build, so the store's location never
  enters an action key.
- **An evictor beside it.**
  [`archive_store_evict.py`](../archive_store/archive_store_evict.py) with
  `--interval`, started before the instance takes an action, as an account that
  can remove every action account's read-only trees, with `--budget-bytes` the
  disk the store may use and `--keep-seconds` the window within which removing a
  tree is logged as thrash. It is standard-library Python 3.14 and needs
  nothing from Bazel. Without it, holds and expansions grow until the next
  start. It exits with an error after repeated failed passes; the supervisor
  should leave that visible rather than restart it forever.
- **A cache that takes `PART_BYTES` blobs.** An archive and its converted tar
  travel as parts of at most `PART_BYTES` (512 MiB,
  [`archive_parts.bzl`](../archive_store/archive_parts.bzl)), so the remote
  cache must accept a blob of that size.
- **System `sh`, `tar` and `shasum`** on every host that runs an action reading
  an archive, and Bun, named by the consumer, on a macOS host that runs one
  reading a linked tree.

## On macOS, how the worker lays out other inputs

The store does not depend on how the executor lays out the rest of an action's
inputs: a mount links to its expansion, and the tree helper clones units itself,
so neither goes through the worker's file fetcher. That fetcher still matters
on a Mac. Upstream `bb_worker` hardlinks inputs from its cache into native
build directories, and with several runners sharing one build directory,
concurrent actions then share an inode per input: macOS can report a running
program's location as another action's hardlink, so a program that locates its
own files relative to its executable reads another action's build directory,
and fails once that action's teardown removes it. A cloning file fetcher that
gives each action its own copy-on-write clone is proposed upstream in
[bb-remote-execution#284](https://github.com/buildbarn/bb-remote-execution/pull/284),
open as of this release; the deployment these rules came from runs that change
as a local patch.

## Why not Buildbarn's NFSv4 build directory

Upstream recommends `bb_worker`'s integrated NFSv4 build directory on macOS,
which lays out no input file on disk and so has neither the per-file cost these
rules remove nor the hardlink problem above. The deployment these rules came
from does not run it, for these reasons, none of which was measured on its
workload:

- **Writable copies of inputs.** Actions that copy a tree to write into it
  copy through the mount, byte by byte, where a native build directory clones.
- **`TMPDIR` inside the build directory** puts scratch space on the mount.
- **Writes go through the worker.** An action's writes are NFS requests that
  `bb_worker` serves into its file pool, whose cost for large build output
  trees, such as Gradle's and Xcode's, is unknown.
- **A worker restart or crash** leaves a process still using the mount without
  the server that answers it; upstream recommends FUSE on Linux in part because
  it handles the server shutting down more gracefully.
- **Untried:** simulators, `codesign` and app installs from an NFS path.

An executor that runs the NFSv4 build directory probably gains less from these
rules, since its per-file layout cost is in the worker's memory rather than in
filesystem calls; that, too, is unmeasured.

## Buildbarn examples

[`examples/buildbarn`](../examples/buildbarn) holds the pieces as a deployment
runs them, with its paths: the runner's environment
([`worker.jsonnet`](../examples/buildbarn/worker.jsonnet)), a Linux worker
unit's start and stop
([`buildbarn-worker.service.d-archive-store.conf`](../examples/buildbarn/buildbarn-worker.service.d-archive-store.conf)),
a Mac worker's launchd program
([`worker-start-macos.sh`](../examples/buildbarn/worker-start-macos.sh)), and the
evictor as a systemd unit
([`archive-store-evict.service`](../examples/buildbarn/archive-store-evict.service))
and a LaunchDaemon
([`com.example.archive-store-evict.plist`](../examples/buildbarn/com.example.archive-store-evict.plist)).
The budget in them is an example; size it to the disk the store may use.

## Without an executor store

On a developer machine, or under any executor that does not set the variable,
each action expands its archives into a private store under `TMPDIR` and
removes it when the command exits. That is correct and reuses nothing, and the
helper logs it on every action.
