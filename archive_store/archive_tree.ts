// Mount a linked tree of declared archives from the worker's archive store,
// building and publishing it there first on a miss, then run the command.
//
// Usage: bun --no-install archive_tree.ts [--root DIR] [--writable] MANIFEST -- COMMAND [ARG...]
//
// A linked tree is a directory holding a clone of each of its units, the
// expansion of one declared archive, at a path of its own, and relative
// symlinks between them (docs/store.md sec."Linked trees").
// archive_mount.sh links one mount per archive, which costs tens of
// milliseconds per archive even when every expansion is warm; a tree of a few
// hundred units would pay that in every action. So the whole tree is published
// once per worker instance, keyed by what it holds, and an action that finds
// it published mounts it with one link and reads no unit. Only on a miss does
// this process expand the units the store lacks, clone each into a staging
// tree with one clonefile(2) of its directory, link them, and publish the tree.
//
// MANIFEST is the tree's manifest, whose format, resolution and key
// archive_tree_manifest.ts owns; a rule writes it with that file's --resolve,
// in a cached action of its own. MOUNT, the manifest's mount path, is
// relative to the working directory, or with --root to DIR, which is then
// exported as ARCHIVE_MOUNT_ROOT. On a miss each archive is checked against
// its digest before it is expanded, as archive_mount.sh checks it. A unit is a
// real directory in the tree, never a link to its expansion, since Bun
// resolves a package's dependencies from its realpath.
//
// The store protocol is archive_mount.sh's, in a namespace named for the
// digest of this file and the manifest's: the claim on the namespace, staging under a name carrying
// this process's identity, publication by symlink(2), holds, use records and
// read-only trees. A tree is published as an entry like an expansion's, so the
// evictor (archive_store_evict.py) holds it to the budget and keeps it while
// a hold names a live reader. The holds name this process, which execs the
// command, so they last exactly as long as the action's root process. With
// --writable the mount is instead one clonefile(2) of the whole tree, made
// writable, for a command that writes into its tree.
//
// This runs under the declared Bun, which a consumer of a linked tree already
// carries to run its bins, so it adds no runtime to the action; Python would
// add its runtime's files to every one. clonefile(2) and execve(2) are called
// through bun:ffi. Trees are built on macOS only: a Linux consumer's tree is
// its declared outputs, which need no store.

import { createHash, randomBytes } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { dlopen, FFIType, ptr, read } from "bun:ffi";
import {
  type Manifest,
  ManifestError,
  readManifest,
  treeKey,
  type Unit,
} from "./archive_tree_manifest.ts";

const PART = /^archive\.tar\.gz\.part-[a-z]{3}$/;
// How many units one miss expands at once: each is a tar of its own.
const EXPANDERS = 8;
// A retirement takes the evictor moments; archive_mount.sh says why a claim
// that outlasts these tries falls back to a private store.
const CLAIMS = 3;
// Reads of one entry that found it unpublished under them before this fails,
// as archive_mount.sh counts them.
const UNPUBLISHED_READS = 3;
// flock(2)'s operations and its busy errno, from macOS's <sys/fcntl.h> and
// <sys/errno.h>.
const LOCK_EX = 2;
const LOCK_NB = 4;
const EWOULDBLOCK = 35;
// How often a builder waiting on another's lock tries it again.
const LOCK_POLL_MS = 20;

class HelperError extends Error {}

function log(message: string): void {
  fs.writeSync(2, `archive_tree: ${message}\n`);
}

function fail(message: string): never {
  throw new HelperError(message);
}

// Each staging tree this process made and has not published, and a private
// store, removed on a failure or a signal; a killed process's staging trees
// are the evictor's to remove.
const staged = new Set<string>();
let privateStore: string | null = null;

function cleanUp(): void {
  for (const tree of staged) discard(tree);
  if (privateStore !== null) discard(privateStore);
}

function discard(tree: string): void {
  makeWritable(tree);
  fs.rmSync(tree, { recursive: true, force: true });
}

function makeWritable(tree: string): void {
  let info: fs.Stats;
  try {
    info = fs.lstatSync(tree);
  } catch {
    return;
  }
  if (info.isSymbolicLink()) return;
  fs.chmodSync(tree, info.mode | 0o200);
  if (info.isDirectory()) {
    for (const name of fs.readdirSync(tree))
      makeWritable(path.join(tree, name));
  }
}

// Whether TARGET, read as a link in DIRECTORY, stays inside ROOT: walked a
// component at a time, resolving each directory as the kernel would, as
// archive_mount.sh's confine_links does. A component that is not a directory
// ends the walk, and what remains of the target may not climb.
function confined(root: string, directory: string, target: string): boolean {
  let here = fs.realpathSync(directory);
  const parts = target.split("/");
  for (let index = 0; index < parts.length; index++) {
    const part = parts[index]!;
    if (part === "" || part === ".") continue;
    const next = path.join(here, part);
    let isDirectory = false;
    try {
      isDirectory = fs.statSync(next).isDirectory();
    } catch {}
    if (!isDirectory) return !parts.slice(index + 1).includes("..");
    here = fs.realpathSync(next);
    if (!`${here}/`.startsWith(`${root}/`)) return false;
  }
  return true;
}

// Refuse a link in the staged expansion that leaves it, then make the
// expansion read-only, as archive_mount.sh does with `chmod -R a+rX,a-w`.
function sealExpansion(root: string, archive: string): void {
  const walk = (directory: string) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const at = path.join(directory, entry.name);
      if (entry.isSymbolicLink()) {
        const target = fs.readlinkSync(at);
        if (target.startsWith("/") || !confined(root, directory, target))
          fail(
            `${archive} holds a symlink that leaves its own tree: ${at} -> ${target}`,
          );
      } else if (entry.isDirectory()) {
        walk(at);
        fs.chmodSync(at, (fs.statSync(at).mode | 0o555) & ~0o222 & 0o7777);
      } else {
        const mode = fs.statSync(at).mode;
        fs.chmodSync(
          at,
          (mode | 0o444 | (mode & 0o111 ? 0o111 : 0)) & ~0o222 & 0o7777,
        );
      }
    }
  };
  walk(root);
  fs.chmodSync(root, 0o555);
}

let libc: ReturnType<typeof openLibc> | undefined;

function openLibc() {
  return dlopen("/usr/lib/libSystem.B.dylib", {
    clonefile: {
      args: [FFIType.ptr, FFIType.ptr, FFIType.u32],
      returns: FFIType.i32,
    },
    execve: {
      args: [FFIType.ptr, FFIType.ptr, FFIType.ptr],
      returns: FFIType.i32,
    },
    utimes: { args: [FFIType.ptr, FFIType.ptr], returns: FFIType.i32 },
    flock: { args: [FFIType.i32, FFIType.i32], returns: FFIType.i32 },
    __error: { args: [], returns: FFIType.ptr },
  }).symbols;
}

function system() {
  if (process.platform !== "darwin")
    fail("linked trees are built on macOS only");
  libc ??= openLibc();
  return libc;
}

function cString(value: string): Buffer {
  return Buffer.from(`${value}\0`);
}

function errno(): number {
  return read.i32(system().__error()!, 0);
}

// One clonefile(2) of a whole directory, its files sharing their blocks.
function clone(source: string, destination: string): void {
  if (
    system().clonefile(ptr(cString(source)), ptr(cString(destination)), 0) !== 0
  )
    fail(`clonefile ${source} -> ${destination} failed with errno ${errno()}`);
}

// This process as an evictor tells it apart: its PID and start time, as `ps`
// prints it in the C locale and UTC, the form archive_mount.sh writes.
function identity(): string {
  const result = Bun.spawnSync(
    ["/bin/ps", "-o", "lstart=", "-p", String(process.pid)],
    {
      env: { PATH: "/usr/bin:/bin", LC_ALL: "C", TZ: "UTC0" },
    },
  );
  const started = result.stdout.toString().replace(/[^0-9A-Za-z]/g, "");
  if (result.exitCode !== 0 || started === "")
    fail("cannot read this process's start time from ps");
  return `${process.pid}-${started}`;
}

// Create FILE if it is missing and set its times to now, as touch(1) does.
// utimes(2) with no times needs only write access, where setting explicit
// times, as fs.utimesSync does, is the owner's alone; another slot's account
// owns most of a shared store's files.
function touch(file: string): void {
  fs.closeSync(fs.openSync(file, "a"));
  if (system().utimes(ptr(cString(file)), null) !== 0)
    fail(`cannot touch ${file}: errno ${errno()}`);
}

function readlink(link: string): string | null {
  try {
    return fs.readlinkSync(link);
  } catch {
    return null;
  }
}

class Store {
  readonly namespace: string;
  // Why the last claim could not make its parts, if it could not.
  claimError = "";
  // Milliseconds spent waiting for another builder's lock.
  waited = 0;

  constructor(
    readonly root: string,
    readonly identity: string,
    namespaceName: string,
  ) {
    this.namespace = path.join(root, namespaceName);
  }

  // archive_mount.sh's claim_namespace: a hold on the namespace taken before
  // the retirement marker is read, so an evictor retiring it either sees the
  // hold or leaves a marker or a missing part this sees.
  claim(): boolean {
    try {
      for (const part of ["objects", "used", "holds"])
        fs.mkdirSync(path.join(this.namespace, part), { recursive: true });
      touch(path.join(this.namespace, ".claims"));
      touch(path.join(this.namespace, "holds", `namespace@${this.identity}`));
    } catch (error) {
      // A retirement between the parts above leaves one missing, which the
      // next try makes afresh; anything else says why a claim fell back.
      this.claimError = (error as Error).message;
      return false;
    }
    return (
      !fs.existsSync(path.join(this.namespace, ".retiring")) &&
      fs.existsSync(
        path.join(this.namespace, "holds", `namespace@${this.identity}`),
      ) &&
      fs.existsSync(path.join(this.namespace, "objects")) &&
      fs.existsSync(path.join(this.namespace, "used"))
    );
  }

  // The object published under KEY, held for this process and its use
  // recorded; null when nothing is published there, or when it was
  // unpublished between the hold and the second read, as an evictor does.
  read(key: string): string | null {
    const entry = path.join(this.namespace, key);
    const object = readlink(entry);
    if (object === null) return null;
    touch(
      path.join(
        this.namespace,
        "holds",
        `${object.replace(/^objects\//, "")}@${this.identity}`,
      ),
    );
    if (readlink(entry) !== object) return null;
    touch(path.join(this.namespace, "used", key));
    return path.join(this.namespace, object);
  }

  // A staging directory no concurrent builder shares, named for KEY and this
  // process, so an evictor can tell a killed builder's tree from one staging.
  stage(key: string): string {
    const staging = fs.mkdtempSync(
      path.join(this.namespace, "objects", `${key}.${this.identity}.`),
    );
    staged.add(staging);
    return staging;
  }

  // Publish STAGING under KEY by symlink(2), which refuses an existing name,
  // so of concurrent builders exactly one publishes and the rest discard
  // theirs. The link is the completion marker.
  publish(key: string, staging: string, what: string): void {
    try {
      fs.symlinkSync(
        `objects/${path.basename(staging)}`,
        path.join(this.namespace, key),
      );
      staged.delete(staging);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
      staged.delete(staging);
      discard(staging);
      log(`discarded a duplicate ${what}: ${key} was published first`);
    }
  }

  // The object under KEY, made by PRODUCE when nothing is published there.
  async obtain(key: string, produce: () => Promise<void>): Promise<string> {
    for (let reads = 0; reads < UNPUBLISHED_READS; reads++) {
      const object = this.read(key);
      if (object !== null) return object;
      await this.exclusively(key, produce);
    }
    return fail(
      `${path.join(this.namespace, key)} was unpublished under every read of it`,
    );
  }

  // Run PRODUCE unless KEY is published, as the one builder of KEY in this
  // store. The first actions on an instance often want one tree at once, and
  // each would otherwise expand every unit and build the tree only to discard
  // it; so builders of one key take flock(2) on `holds/<key>.lock` in turn, and
  // each that finds the key published once it has the lock leaves it there.
  // The lock goes once the key is published, while still locked, so a later
  // builder of it reads the entry instead; a waiter on the removed file gets
  // its lock and reads the entry too. The evictor reads only `@`-named holds,
  // so it leaves the lock alone, and a lock a failed builder left stays until
  // the key is next published (docs/store.md sec."Linked trees").
  async exclusively(key: string, produce: () => Promise<void>): Promise<void> {
    const entry = path.join(this.namespace, key);
    const lock = path.join(this.namespace, "holds", `${key}.lock`);
    const fd = fs.openSync(
      lock,
      fs.constants.O_RDONLY | fs.constants.O_CREAT,
      0o664,
    );
    try {
      const waiting = performance.now();
      // Without blocking, since one process expands several units at once.
      while (system().flock(fd, LOCK_EX | LOCK_NB) !== 0) {
        if (errno() !== EWOULDBLOCK)
          fail(`cannot lock ${lock}: errno ${errno()}`);
        await Bun.sleep(LOCK_POLL_MS);
      }
      this.waited += performance.now() - waiting;
      if (readlink(entry) === null) await produce();
      fs.rmSync(lock, { force: true });
    } finally {
      // Which releases the lock.
      fs.closeSync(fd);
    }
  }
}

// Expand a unit's archive, its parts joined in name order, after checking
// their digest, as archive_mount.sh's expand does.
async function expand(store: Store, unit: Unit): Promise<void> {
  const parts = fs
    .readdirSync(unit.archive)
    .filter((name) => PART.test(name))
    .sort();
  if (parts[0] !== "archive.tar.gz.part-aaa")
    fail(
      `${unit.archive} is not the tar declared_archive writes; declare the archive through declared_archive`,
    );
  const files = parts.map((name) => path.join(unit.archive, name));
  const hash = createHash("sha256");
  for (const file of files)
    for await (const chunk of Bun.file(file).stream()) hash.update(chunk);
  const actual = hash.digest("hex");
  if (actual !== unit.digest)
    fail(
      `${unit.archive} has digest ${actual}, but the manifest declares ${unit.digest}`,
    );
  const staging = store.stage(unit.digest);
  // An empty environment, so TAR_OPTIONS in the action's cannot add options.
  const tar = Bun.spawn(
    ["/usr/bin/tar", "-xzf", "-", "-C", staging, "--no-same-owner"],
    {
      env: { PATH: "/usr/bin:/bin" },
      stdin: "pipe",
      stderr: "pipe",
    },
  );
  for (const file of files) {
    for await (const chunk of Bun.file(file).stream()) {
      tar.stdin.write(chunk);
      await tar.stdin.flush();
    }
  }
  await tar.stdin.end();
  if ((await tar.exited) !== 0)
    fail(
      `tar could not expand ${unit.archive}: ${await new Response(tar.stderr).text()}`,
    );
  sealExpansion(staging, unit.archive);
  store.publish(unit.digest, staging, `expansion of ${unit.archive}`);
}

interface Built {
  expanded: number;
  reused: number;
}

// Expand every unit the store lacks, clone each unit into a staging tree and
// link them, then publish the tree under KEY.
async function build(
  store: Store,
  key: string,
  manifest: Manifest,
  built: Built,
): Promise<void> {
  const byDigest = new Map<string, Unit>();
  for (const unit of manifest.units) byDigest.set(unit.digest, unit);
  const expansions = new Map<string, string>();
  const pending = [...byDigest.values()];
  const work = async () => {
    for (let unit = pending.shift(); unit; unit = pending.shift()) {
      let produced = false;
      const object = await store.obtain(unit.digest, async () => {
        produced = true;
        await expand(store, unit);
      });
      expansions.set(unit.digest, object);
      if (produced) built.expanded++;
      else built.reused++;
    }
  };
  await Promise.all(
    Array.from({ length: Math.min(EXPANDERS, pending.length) }, work),
  );

  const staging = store.stage(key);
  // The directories the tree makes for its units and links, made read-only
  // once the tree is whole, deepest first.
  const made: string[] = [];
  const parent = (at: string) => {
    let directory = staging;
    for (const part of path.dirname(at).split("/")) {
      if (part === ".") break;
      directory = path.join(directory, part);
      if (!fs.existsSync(directory)) {
        fs.mkdirSync(directory);
        made.push(directory);
      }
    }
  };
  for (const unit of manifest.units) {
    const source = path.join(expansions.get(unit.digest)!, unit.strip);
    if (!fs.statSync(source, { throwIfNoEntry: false })?.isDirectory())
      fail(`${unit.dest}: ${unit.archive} has no directory ${unit.strip}`);
    parent(unit.dest);
    clone(source, path.join(staging, unit.dest));
  }
  // Before the links, so each FILE is read inside its unit's clone and never
  // through a link. A clone's file is its own inode, so the shared expansion
  // keeps its mode.
  for (const file of manifest.executables) {
    // A directory link inside the unit's archive could carry the path out of
    // the clone, so the file's real path must lie inside its unit's.
    const unit = manifest.units.find((u) => file.startsWith(`${u.dest}/`))!;
    const at = path.join(staging, file);
    const info = fs.lstatSync(at, { throwIfNoEntry: false });
    if (!info?.isFile())
      fail(`executable ${file} is not a regular file in its unit`);
    const clone = fs.realpathSync(path.join(staging, unit.dest));
    if (!fs.realpathSync(at).startsWith(`${clone}/`))
      fail(`executable ${file} leaves its unit ${unit.dest}`);
    fs.chmodSync(at, (info.mode | 0o111) & 0o7777);
  }
  for (const link of manifest.links) {
    parent(link.at);
    fs.symlinkSync(link.target, path.join(staging, link.at));
  }
  const root = fs.realpathSync(staging);
  for (const link of manifest.links) {
    if (!confined(root, path.dirname(path.join(root, link.at)), link.target))
      fail(`link ${link.at} -> ${link.target} leaves the tree`);
  }
  for (const directory of [...made.reverse(), staging])
    fs.chmodSync(directory, 0o555);
  store.publish(key, staging, "tree");
}

// Mount OBJECT at MOUNT: a link to it, or with WRITABLE a writable clone.
function mount(
  object: string,
  at: string,
  base: string,
  writable: boolean,
): void {
  // A symlink above the mount, left in a shared execution root, would carry
  // the mount somewhere else.
  const parts = at.split("/");
  for (let depth = 1; depth < parts.length; depth++) {
    const above = path.join(base, ...parts.slice(0, depth));
    if (fs.lstatSync(above, { throwIfNoEntry: false })?.isSymbolicLink())
      fail(`${above}, above mount ${at}, is a symlink`);
  }
  const target = path.join(base, at);
  // The writable clone this helper made beside the mount, by its name, then,
  // once cloned, also by device and inode, so a rerun of the action in a
  // shared execution root removes exactly what an earlier run left: a staging
  // clone a killed run never renamed, or the clone at the mount, and no other
  // directory. It is never followed: a link at its path is a collision, it is
  // read and rewritten without following one, and it is made exclusively.
  const marker = `${target}.writable`;
  const marked = fs.lstatSync(marker, { throwIfNoEntry: false });
  if (marked && !marked.isFile())
    fail(`${marker} is not a marker this helper wrote`);
  const owned = marked ? readMarker(marker, path.basename(target)) : null;
  if (owned?.staging !== undefined) {
    const abandoned = path.join(path.dirname(target), owned.staging);
    if (fs.lstatSync(abandoned, { throwIfNoEntry: false })?.isDirectory())
      discard(abandoned);
  }
  const existing = fs.lstatSync(target, { throwIfNoEntry: false });
  // What an earlier run of this action left in a shared execution root, a link
  // or the writable clone its marker names, is replaced; anything else at the
  // mount path is a collision.
  if (existing?.isSymbolicLink()) fs.unlinkSync(target);
  else if (
    existing?.isDirectory() &&
    owned?.clone !== undefined &&
    owned.clone === inode(existing)
  )
    discard(target);
  else if (existing) fail(`${at} already exists in the input root`);
  if (marked) fs.unlinkSync(marker);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  if (writable) {
    // Cloned beside the mount, named in the marker first, and renamed onto
    // the mount once the marker also names its inode, so the mount path never
    // holds a clone its marker does not name, and a staging clone a killed
    // run leaves is the next run's to remove.
    const staging = `${target}.clone.${randomBytes(6).toString("hex")}`;
    writeMarker(marker, [path.basename(staging)], true);
    // Removed on a failure or a signal until the rename lands, as a staging
    // tree in the store is.
    staged.add(staging);
    clone(object, staging);
    makeWritable(staging);
    writeMarker(
      marker,
      [path.basename(staging), inode(fs.lstatSync(staging))],
      false,
    );
    fs.renameSync(staging, target);
    staged.delete(staging);
  } else {
    fs.symlinkSync(object, target);
  }
}

function inode(info: fs.Stats): string {
  return `${info.dev}:${info.ino}`;
}

// Write the marker's lines: made exclusively at first, then rewritten in
// place, never through a link.
function writeMarker(marker: string, lines: string[], create: boolean): void {
  const fd = fs.openSync(
    marker,
    fs.constants.O_WRONLY |
      fs.constants.O_NOFOLLOW |
      (create
        ? fs.constants.O_CREAT | fs.constants.O_EXCL
        : fs.constants.O_TRUNC),
    0o644,
  );
  try {
    fs.writeSync(fd, lines.map((line) => `${line}\n`).join(""));
  } finally {
    fs.closeSync(fd);
  }
}

// The marker's staging clone name, which must be one this helper makes for
// MOUNT, and the clone's inode once it was recorded.
function readMarker(
  marker: string,
  mount: string,
): { staging?: string; clone?: string } {
  const fd = fs.openSync(
    marker,
    fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW,
  );
  let text: string;
  try {
    text = fs.readFileSync(fd, "ascii");
  } finally {
    fs.closeSync(fd);
  }
  const [staging, clone] = text.split("\n");
  const named = `${mount}.clone.`;
  return {
    staging:
      staging?.startsWith(named) &&
      /^[0-9a-f]{12}$/.test(staging.slice(named.length))
        ? staging
        : undefined,
    clone: clone || undefined,
  };
}

// Replace this process with the command, which keeps its PID and start time,
// so the holds above last exactly as long as it does.
function exec(
  command: string[],
  environment: Record<string, string | undefined>,
  searchPath: string,
): never {
  let file = command[0]!;
  if (!file.includes("/")) {
    const found = searchPath
      .split(":")
      .map((directory) => path.join(directory || ".", file))
      .find((candidate) => {
        try {
          fs.accessSync(candidate, fs.constants.X_OK);
          return fs.statSync(candidate).isFile();
        } catch {
          return false;
        }
      });
    if (found === undefined) fail(`${file} is not on PATH`);
    file = found;
  }
  const strings = (values: string[]) => values.map(cString);
  const pointers = (buffers: Buffer[]) => {
    const array = new BigUint64Array(buffers.length + 1);
    buffers.forEach((buffer, index) => (array[index] = BigInt(ptr(buffer))));
    return array;
  };
  const argv = strings(command);
  const envp = strings(
    Object.entries(environment).flatMap(([name, value]) =>
      value === undefined ? [] : [`${name}=${value}`],
    ),
  );
  const path_ = cString(file);
  system().execve(ptr(path_), ptr(pointers(argv)), ptr(pointers(envp)));
  return fail(`cannot run ${file}: errno ${errno()}`);
}

async function main(argv: string[]): Promise<number> {
  const started = performance.now();
  const usage =
    "usage: archive_tree.ts [--root DIR] [--writable] MANIFEST -- COMMAND [ARG...]";
  let base = process.cwd();
  let writable = false;
  const environment = { ...process.env };
  for (;;) {
    if (argv[0] === "--root" && argv.length >= 2) {
      fs.mkdirSync(argv[1]!, { recursive: true });
      base = fs.realpathSync(argv[1]!);
      environment.ARCHIVE_MOUNT_ROOT = base;
      argv = argv.slice(2);
    } else if (argv[0] === "--writable") {
      writable = true;
      argv = argv.slice(1);
    } else {
      break;
    }
  }
  if (argv.length < 3 || argv[1] !== "--") fail(usage);
  const manifest = readManifest(argv[0]!);
  const command = argv.slice(2);
  const key = treeKey(manifest);
  system();

  // Every slot's account shares the store through the action group; the
  // caller's umask comes back before the command runs.
  const callerUmask = process.umask(0o002);
  const self = identity();
  // The manifest's reading and key decide what a tree holds, so a change to
  // either file starts a namespace of its own.
  const namespaceName = createHash("sha256")
    .update(fs.readFileSync(import.meta.path))
    .update(
      fs.readFileSync(new URL("./archive_tree_manifest.ts", import.meta.url)),
    )
    .digest("hex")
    .slice(0, 16);
  const usePrivateStore = () => {
    privateStore = fs.realpathSync(
      fs.mkdtempSync(path.join(process.env.TMPDIR || "/tmp", "archive-store.")),
    );
    return privateStore;
  };
  const configured = process.env.RULES_ARCHIVE_STORE_DIR;
  let store: Store;
  if (configured) {
    if (!fs.statSync(configured, { throwIfNoEntry: false })?.isDirectory())
      fail(`RULES_ARCHIVE_STORE_DIR=${configured} is not a directory`);
    store = new Store(fs.realpathSync(configured), self, namespaceName);
    for (let claims = 1; !store.claim(); claims++) {
      if (claims >= CLAIMS) {
        log(
          `cannot claim ${store.namespace} in the shared store${store.claimError ? ` (${store.claimError})` : ""}; building into a private store`,
        );
        store = new Store(usePrivateStore(), self, namespaceName);
        if (!store.claim()) fail(`cannot make ${store.namespace}`);
        break;
      }
      Bun.sleepSync(1000);
    }
  } else {
    store = new Store(usePrivateStore(), self, namespaceName);
    log(
      `no RULES_ARCHIVE_STORE_DIR; building into private store ${store.root}`,
    );
    if (!store.claim()) fail(`cannot make ${store.namespace}`);
  }

  const built: Built = { expanded: 0, reused: 0 };
  let missed = false;
  const object = await store.obtain(key, async () => {
    missed = true;
    await build(store, key, manifest, built);
  });
  mount(object, manifest.mount, base, writable);
  const elapsed = Math.round(performance.now() - started);
  // The host names the store, which belongs to one worker instance.
  const where = `in ${store.root} on ${os.hostname()}`;
  log(
    missed
      ? `built tree ${key} ${where} in ${elapsed} ms: expanded ${built.expanded} of ${built.expanded + built.reused} archives, cloned ${manifest.units.length} units, made ${manifest.links.length} links`
      : `mounted published tree ${key} ${where} in ${elapsed} ms, reading no unit${store.waited >= 1 ? ` after waiting ${Math.round(store.waited)} ms for its builder` : ""}`,
  );

  process.umask(callerUmask);
  if (privateStore !== null) {
    // The private store is removed once the command exits, so it runs as a
    // child rather than in this process's place.
    const child = Bun.spawn(command, {
      cwd: process.cwd(),
      env: environment,
      stdio: ["inherit", "inherit", "inherit"],
    });
    const status = await child.exited;
    cleanUp();
    return child.signalCode
      ? 128 + (os.constants.signals[child.signalCode] ?? 0)
      : status;
  }
  return exec(command, environment, process.env.PATH ?? "");
}

for (const signal of ["SIGHUP", "SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => {
    cleanUp();
    process.exit(128 + os.constants.signals[signal]);
  });
}

try {
  process.exitCode = await main(process.argv.slice(2));
} catch (error) {
  cleanUp();
  log(
    `error: ${error instanceof HelperError || error instanceof ManifestError ? error.message : ((error as Error).stack ?? String(error))}`,
  );
  process.exitCode = 1;
}
