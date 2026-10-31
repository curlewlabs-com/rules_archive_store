# The archive store

A toolchain, SDK or dependency tree declared as its archive reaches an action as
one input. This page is the mechanism: how a tree is declared as its archive,
and how an executor instance expands each archive once and reuses the
expansion. What an executor has to provide for that is
[executor.md](executor.md).

## Declaring and reading a tree

[`archives.bzl`](../archive_store/archives.bzl) supplies the pieces.

- **`declared_archive`** names an archive, tar or zip, the directory inside it
  that is the tree's root, and its SHA-256. A pinned upstream archive takes the
  digest its lock already pins, which Bazel verified when it downloaded the
  archive; an npm tarball takes its lock's SHA-512 `integrity`, and strips a
  leading component (`strip_components`) where npm does. An archive the build
  produces takes a digest file its producer writes in the same action, so the
  two cannot disagree. Every archive is converted, in a cached action of its own
  ([`archive_to_tar.py`](../archive_store/archive_to_tar.py)), into a
  reproducible tar: the conversion checks the archive against its digest, and
  refuses any member or link that would land outside the tree or beneath one of
  the archive's own links, judging paths as a case-insensitive macOS volume
  does: in NFD, which the Apple File System Reference sets for a name's hash,
  and case-folded. A name holding a control character, bytes that are not UTF-8
  or a character the interpreter's Unicode tables do not know is refused, since
  its folding cannot be checked. APFS leaves its case folding unstated, so the
  conversion's macOS test has the volume itself report which names it merges,
  per code point. That test runs uncached, since the volume is no input Bazel
  sees, and fails on a case-sensitive volume, which would merge nothing. The
  conversion then writes the converted tar's digest, which is what consumers key
  the store by. Only `declared_archive` creates the `ArchiveInfo` a mount reads,
  so the helper extracts only tars the build wrote and checked. A `drop` pattern
  leaves out members a case-insensitive volume could not hold, such as headers
  differing only in case, and lists them in `<name>.dropped.txt`, whose digest
  `dropped_sha256` pins. A dropped member's name and type are still checked; it
  skips only the duplicate, case and link checks, since it is never written.
- **An archive larger than a cache blob travels as ordered parts.** A remote
  cache refuses a blob larger than it is configured to hold, and an SDK archive
  can outgrow that. A repository that downloads such an archive does it through
  [`archive_parts.bzl`](../archive_store/archive_parts.bzl)'s
  `download_archive_parts` or its `archive_parts` rule: it fetches the whole
  archive, splits it by byte count into parts of at most `PART_BYTES` named
  `<archive>.part-aaa`, `.part-aab` and on, and only then deletes the whole
  file, so while it splits the repository holds the archive twice over.
  `declared_archive` takes the parts as its `archive`, in name order, and the
  conversion reads them as one stream
  ([`archive_parts.py`](../archive_store/archive_parts.py)) and checks the lock's
  digest over all of it. The conversion writes its tar as parts of at most
  `PART_BYTES` too, into a directory, and the helper pipes them in name order
  into one `tar`, after checking the tar's digest over the same parts, joined,
  so no whole copy of a large tar is ever written. An archive no larger than
  `PART_BYTES` is one part.
- **`archive_mounts`** prepares an action that reads one or more archives. The
  action runs its command through
  [`archive_mount.sh`](../archive_store/archive_mount.sh), which links each
  mount path in the input root to the archive's expansion and then runs the
  command. The command reads the tree in place at an execution-root path, beside
  the call's own manifest, so each call has a mount root of its own, and an
  action that alone runs it keeps that root even in an unsandboxed execution
  root.
- **`archive_test`** runs a test with archives mounted. A test reads from its
  runfiles tree, so its manifest names the archives by runfiles path, while its
  mounts go under its own temporary directory: the helper takes `--root` for
  that and names the directory to the test in `ARCHIVE_MOUNT_ROOT`. The helper
  execs the test, as it does an action's command, so the test is the process
  whose holds keep its trees from eviction for as long as it runs. Mounting
  counts against the test's timeout, so the launcher exports the epoch second it
  started in `ARCHIVE_TEST_STARTED` for a test that keeps its own deadline. A
  rule whose mounts come from a provider rather than a dict written in BUILD
  builds the same launcher through `archive_test_launcher`. Such a wrapper
  launches the test's executable directly, so the test's own `args` never reach
  it; they go on the wrapper, and what they name goes in its `data`, which makes
  each label one `$(rootpath)` may expand, puts its files in the runfiles, and
  expands `env` against. `archive_test_launcher`'s `tree` puts a linked tree at
  `tree` under the same root: on macOS the mount helper's command is the tree
  helper's, which mounts the published tree there and execs the test, so the
  test's process holds every mount; elsewhere, where a tree can be a directory
  of the test's runfiles, a shell links it there and execs the test. An archive
  mounted at `tree`, under it, or at the tree helper's `tree.writable` marker,
  in any case, would collide with it, so the launcher refuses one.
- **A `bazel run` binary** that reads archives has no `TEST_TMPDIR`, so
  `archive_binary` and `archive_run_launcher` make its mount root with
  `mktemp -d` and run the helper as a child, removing the root once the helper
  returns. `archive_tree_run_launcher` launches a binary with a linked tree the
  same way. The binary runs by a relative path from the `_main` directory of the
  launcher's runfiles, and `bazel run` sets no `RUNFILES_DIR`, so a shell binary
  finds its runfiles from that directory, not from `$0.runfiles`. The root holds
  only mount links, so removing it never touches the store. With a shared store
  the helper execs the binary, so its holds name the live process. Outside an
  executor that names a store, each run expands its archives into a private
  store (see The store): the helper then stays the binary's parent, removes that
  store when the binary exits, and passes its status on. In a GitHub Actions job
  the launcher points `TMPDIR` at the job's `RUNNER_TEMP` first, so the private
  store is outside a sweep that ages `/tmp` by mtime alone, which would take an
  expansion's zero-mtime tree mid-run.
- **A toolchain another ruleset runs**, such as a linker another ruleset
  invokes, cannot wrap that ruleset's action, so its tool paths can name
  generated launchers that run the helper and then the tool. Its mount root then
  belongs to the toolchain, shared by every action of a configuration that runs
  one of its tools, so it relies on each action having an input root of its
  own, as a remote worker's build directory and a sandbox give it.

An action reads a tree at its mount path; where the mount leads differs between
executor instances. So no output may embed a tree's resolved location, or the
output would differ by which instance built it, and a tree is the archive as
acquired: `drop` is for names a case-insensitive volume cannot hold and nothing
built reads, never a way to shrink a tree.

The helper is POSIX `sh` over system `tar` and `shasum`. It runs inside every
action that reads an archive, so whatever it needs is an input to all of them.
It depends on no declared tree: one kept as its files would put its per-file
layout back into every such action, and one declared as an archive would need
the helper to expand it first. It runs `tar` with an empty environment and its
own `cd` without the caller's `CDPATH`, so an action's environment cannot change
what it extracts or where. The system shell and utilities are therefore
dependencies of every executor that runs one.

## The store

The helper expands an archive into the store of the executor instance running
the action, keyed by the archive's digest (see Key below), and later actions on
that instance reuse the expansion.

- **Where it is.** `RULES_ARCHIVE_STORE_DIR` names the store. Whatever runs the
  action sets it outside the action's key, as a Buildbarn worker's per-runner
  environment in its own configuration does, so the store's location never
  splits the cache. The store sits on the build directory's volume, so an
  expansion can be cloned into an action's directory, which a clone across
  volumes cannot do. When the variable is unset, as on a developer machine, the
  helper expands into a private store and removes it when the command exits.
  That is correct but reuses nothing, and the helper logs that it did it.
- **Emptied at each start.** Whatever sets the variable empties the store before
  its instance starts. Nothing flushes an expansion to disk before publishing
  it, so one that a crash interrupted can be published but truncated; a crash
  restarts the instance, and the start discards it. The first actions after a
  restart therefore expand their archives again.
- **Owned by one instance.** Nothing is shared between instances, and nothing
  locks across them. One instance's actions can still ask for the same archive
  at once, which the publication protocol below handles.
- **Key.** An expansion is keyed by the archive's digest, inside a namespace
  named for the helper's own digest, so a helper revision that extracts or
  checks differently never reuses a tree an earlier one published.
- **Publication.** An expander checks the archive against its digest again,
  extracts it into a staging directory of its own, refuses it if any symlink in
  it resolves outside the tree, makes it read-only and publishes it by creating
  a symlink named for its key. `symlink(2)` refuses an existing name, so of
  several concurrent expanders exactly one publishes and the rest discard their
  copies. The published link is the completion marker: an expansion killed
  before it leaves only an unpublished staging directory, which no reader
  follows.
- **Read-only.** An expansion's files and directories carry no write bit, so a
  tool that writes into its tree fails instead of changing what the next action
  reads. That guards against accident, not intent: the account that expanded a
  tree owns it and could restore the write bit, and sibling accounts share the
  store's group. Actions on one executor instance are trusted with each other's
  trees, as a shared build directory already assumes. A consumer that writes
  into a tree needs a writable copy of it, which `archive_mount.sh` does not
  make; a linked tree's reader can ask for one ([Linked trees](#linked-trees)).
- **Digest.** A warm expansion is reused by digest without hashing the archive
  again, which on every action would cost what the store saves. The conversion
  writes the digest of the tar it wrote, only once the declared archive matched
  its declared digest, and consumers read the digest from that output, so
  Bazel's cache key ties each digest to the tar's bytes, and the hashing happens
  once per archive version.
- **Failure.** An expansion that fails before publication removes its staging
  directory, so a bad archive cannot fill the store one retry at a time.
- **Use.** Each read touches a per-key file under the namespace's `used`
  directory, so eviction can take the least recently used expansions.
- **Holds.** A read takes a hold on the object it mounts: a file under the
  namespace's `holds` directory named for the object and for the reading
  process's PID and start time, as `ps` prints it. The helper execs the action's
  command, which keeps its PID and start time, so a hold lasts as long as the
  action's root process. The helper never removes one, since every mount of an
  object in one action shares its hold; an evictor drops each hold whose process
  has gone, and until one runs, holds accumulate one per object an action reads.
  The read takes its hold and then checks that the entry still names the object;
  an evictor must unpublish an entry before it reads the holds on its object, so
  it sees every hold a read relied on, and a read that finds the entry gone
  takes the archive afresh. A staging directory's name carries its creator's
  identity the same way, so a killed expansion's tree can be told from one still
  being staged.
- **Claiming a namespace.** Before it stages or mounts anything, the helper
  makes its namespace's directories, marks it `.claims`, and takes a hold on the
  namespace itself, then checks that the namespace carries no `.retiring` marker
  and that its hold is still where it put it. An evictor retiring the namespace
  (see Eviction) marks it before it reads it for the last time, so either that
  read sees the hold and keeps the namespace, or the helper sees the marker or
  finds its hold renamed away, or a part of the namespace missing because a
  retirement fell between the directories it made, and tries again, making the
  namespace afresh. A marker that outlasts a few tries was left by an evictor
  that died mid-retirement, so the helper expands that action's archives into a
  private store instead of failing it.

## Linked trees

A dependency tree of many small archives, such as an npm consumer's packages,
would cost an action one `archive_mount.sh` mount per archive, tens of
milliseconds each even when every expansion is warm. So the store publishes the
whole linked tree once per executor instance, and an action that finds it mounts
it with one link. [`archive_tree.ts`](../archive_store/archive_tree.ts) is the
helper, and `archives.bzl`'s `archive_tree` prepares an action that reads a
tree, as `archive_mounts` does for archives.

- **What a tree is.** A directory holding a clone of each unit, the expansion of
  one `declared_archive`, at a path of its own, and relative symlinks among
  them. A unit is a real directory in the tree, never a link to its expansion,
  since Bun resolves a package's dependencies from its realpath. The manifest
  can name files inside a unit's clone that the tree marks executable, such as a
  package's bins; the clone's file is its own inode, so the shared expansion
  keeps the archive's mode.
- **Its manifest.** The units, each by its archive and digest, and the links, in
  the format
  [`archive_tree_manifest.ts`](../archive_store/archive_tree_manifest.ts) owns.
  The digests are written into it by a cached action of its own, so an action
  reading the tree takes its archives and one manifest, and reads no digest file
  per unit.
- **Key.** The SHA-256 of what the tree holds: each unit's path, digest and
  strip prefix, each link, and each file it marks executable. Never a path in
  the manifest, which differs by configuration, so every configuration's readers
  share one tree.
- **Lookup first.** On a hit the helper mounts the published tree with one link,
  reading no unit.
- **One process on a miss.** It expands each unit the store lacks, checked,
  staged, confined and published as `archive_mount.sh` expands an archive. It
  then clones each unit into a staging tree with one `clonefile(2)` of its
  directory, makes the links, refusing one that leaves the tree, makes the tree
  read-only and publishes it under its key by `symlink(2)`. A directory's clone
  returns one errno for its whole hierarchy, so a clone that fails says what it
  left: whether the source, the destination's parent and the destination
  exist, and, when they are as the call needed them, the first entry that fails
  when cloned alone.
- **One builder at a time.** The first actions on an instance often want one
  tree at once, and each expanding every unit only to discard the tree cost
  every one of them the slowest build. So builders of one key, a tree's or a
  unit's, take `flock(2)` on `holds/<key>.lock` in turn, and each that finds the
  key published once it has the lock reads it. The lock is removed once its key
  is published; the evictor reads only `@`-named holds and leaves it alone, and
  one a failed builder left stays until the key is next published.
- **Holds and eviction.** A tree is published as an entry of the helper's
  namespace, its object named for its builder as a staging expansion is, and
  each reader holds it and records its use as `archive_mount.sh` does, so the
  evictor treats trees as it treats expansions: least recently used first, and
  never one a live reader holds. The helper execs the command through `bun:ffi`,
  so its holds last as long as the action's root process. The budget counts a
  clone's blocks in full, as APFS reports them, so a tree and the expansions it
  was cloned from count twice while the store keeps the tree and those
  expansions: eviction comes earlier than the disk requires, never under a
  reader.
- **Writable.** A reader that writes into its tree gets one `clonefile(2)` of
  the whole tree, made writable, instead of the link. The clone is made beside
  the mount and renamed onto it, under a marker the helper creates exclusively
  and never follows, which names the staging clone before it is made and its
  device and inode once it is. So a rerun of the action in a shared execution
  root removes exactly what an earlier run left - a staging clone a killed run
  never renamed, or the clone its marker names at the mount - and no other
  directory; anything else at the mount path is a collision.
- **Where it runs.** On macOS only, for `clonefile(2)`, under the Bun the
  consumer names through `archive_tree_attrs`, which a tree's consumer already
  carries to run its bins, so it adds no runtime to the action. Its namespace
  is named for the digest of the helper and the manifest's reader together,
  apart from `archive_mount.sh`'s, and claimed as that helper claims its own. It
  sets a shared file's times with `utimes(2)` and no times, as `touch` does,
  since setting explicit times is the file's owner's alone, and another
  account's actions may own most of a store's files.

## Eviction

[`archive_store_evict.py`](../archive_store/archive_store_evict.py) holds a
store to a byte budget. While the store is over it, each pass removes the least
recently used expansions and linked trees, whatever their age, until the store
fits. Only a live hold keeps a tree, so a store whose every tree is held stays
over budget until those actions finish, and the pass logs that. Removing a tree
read within the keep window is logged as thrash, a sign the budget is smaller
than what the instance's actions read. The evictor unpublishes an entry before
it reads the holds on its object, as Holds above requires, and renames a tree
out of `objects` before deleting any of it, so no reader sees a tree
half-deleted; a delete cut short is finished by the next pass, and nothing new
goes into a trash that could not be emptied. It drops each hold whose process
has gone, and removes an unpublished tree once nothing holds it and its creator
has gone. It judges liveness from one `ps` listing per pass, taken after every
namespace's holds and objects are read, plus one for each eviction's check of
its holds, and stops a pass at the first judgment `ps` cannot support. It runs
as an account that can remove every action account's read-only trees, one pass
at a time per store, and reaches the store only through the directory it
opened, so an instance start that sets the store aside and makes a new one
leaves the pass on the old store and never touches the new one.

Each helper revision makes a namespace of its own, so the evictor retires a
namespace with no object, entry or hold left in it, the namespace hold of
Claiming a namespace included: it marks the namespace `.retiring`, reads it
again, and renames it into the trash only if it is still empty. It retires only
a namespace marked `.claims`, and leaves alone one with no `holds` directory,
since nothing in either shows whether a reader still needs it.

The budget counts trees, not the namespaces' own directories, entries, use
records and holds. Those are bounded without it: an entry and its use record
come and go with the tree the budget counts, a hold outlives its process only
until the next pass, and a namespace goes once it is empty. A directory's blocks
need not shrink when entries leave it, so counting them would have the evictor
remove trees for bytes their removal never returns.

After repeated failed passes the evictor exits with an error, so whatever
supervises it can report it rather than restart it unnoticed while the store
grows.

### Releases sharing a store

One executor instance can run actions from builds that pin different releases
of these rules, while its evictor is the release its deployment installed, so
one store holds the namespaces of several helper revisions and an evictor of
one release judges them all. Each helper revision makes a namespace of its own,
so no helper reads another revision's expansions. What an evictor reads in a
namespace is this protocol: the `.claims` and `.retiring` markers, the `holds`
directory and the names of its holds, the `used` records, the published entries
and the objects they name, and the staging names that carry a creator. A release
keeps that readable by the evictors of earlier releases. A change an earlier
evictor would misread goes in a namespace with no `holds` directory, which an
earlier evictor counts and leaves whole, so that release's trees go only at the
instance's next start until its own evictor is deployed; its release notes say
so.
