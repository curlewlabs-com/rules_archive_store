// The manifest of a linked tree (archive_tree.ts): its format, how a rule
// resolves its digests, how it is read, and the key the tree is published
// under (docs/store.md sec."Linked trees").
//
// Usage: bun --no-install archive_tree_manifest.ts --resolve TEMPLATE MANIFEST
//
// MANIFEST holds one entry per line, fields separated by `|`:
//
//   mount|MOUNT
//   unit|DEST|DIGEST|ARCHIVE|STRIP
//   link|PATH|TARGET
//   exec|FILE
//
// MOUNT and ARCHIVE are as archive_mount.sh reads them. DIGEST is the
// archive's lowercase hex SHA-256. A rule writes MANIFEST with --resolve, in a
// cached action of its own, from a TEMPLATE whose DIGEST fields are `@PATH`,
// each naming the file whose first word is that digest, so an action reading
// the tree reads one file, not a digest file per unit, and takes no digest file
// as an input. DEST is where the unit's directory, STRIP inside its archive, is
// cloned in the tree, and PATH where a link to TARGET is made; each is a
// normalized relative path from the tree's root, none inside another. FILE is
// a regular file inside a unit's clone that the tree marks executable whatever
// mode its archive gave it, such as a package's bin.
//
// The tree is keyed by the SHA-256 of what it holds: each unit's DEST, digest
// and STRIP, each link, and each FILE. Never by a path in the manifest, which differs by
// configuration, so every configuration's readers of one tree share it.

import { createHash } from "node:crypto";
import fs from "node:fs";

export class ManifestError extends Error {}

function fail(message: string): never {
  throw new ManifestError(message);
}

export interface Unit {
  dest: string;
  digest: string;
  archive: string;
  strip: string;
}

export interface Link {
  at: string;
  target: string;
}

export interface Manifest {
  mount: string;
  units: Unit[];
  links: Link[];
  executables: string[];
}

function checkRelative(what: string, value: string): void {
  if (
    value === "" ||
    value.startsWith("/") ||
    value
      .split("/")
      .some((part) => part === "" || part === "." || part === "..")
  )
    fail(`${what} '${value}' is not a normalized relative path`);
}

function checkDigest(value: string): string {
  if (!/^[0-9a-f]{64}$/.test(value))
    fail(`digest '${value}' is not a lowercase hex SHA-256`);
  return value;
}

// Write TEMPLATE to MANIFEST with each unit's `@PATH` digest replaced by the
// first word of the file it names.
export function resolve(template: string, manifest: string): void {
  const lines = fs
    .readFileSync(template, "utf8")
    .split("\n")
    .map((line) => {
      const fields = line.split("|");
      if (fields[0] !== "unit" || !fields[2]?.startsWith("@")) return line;
      const named = fs.readFileSync(fields[2].slice(1), "ascii");
      fields[2] = checkDigest((named.split("\n")[0] ?? "").split(" ")[0]!);
      return fields.join("|");
    });
  fs.writeFileSync(manifest, lines.join("\n"));
}

export function readManifest(file: string): Manifest {
  let mount: string | undefined;
  const units: Unit[] = [];
  const links: Link[] = [];
  const executables: string[] = [];
  // Every DEST and PATH, case-folded as a macOS volume compares them.
  const placed = new Set<string>();
  const place = (what: string, at: string) => {
    checkRelative(what, at);
    const folded = at.toLowerCase();
    if (placed.has(folded)) fail(`${what} '${at}' is given twice`);
    placed.add(folded);
  };
  for (const line of fs.readFileSync(file, "utf8").split("\n")) {
    if (line === "") continue;
    const [kind, ...fields] = line.split("|");
    if (kind === "mount" && fields.length === 1 && mount === undefined) {
      checkRelative("mount", fields[0]!);
      mount = fields[0]!;
    } else if (kind === "unit" && fields.length === 4) {
      const [dest, digest, archive, strip] = fields as [
        string,
        string,
        string,
        string,
      ];
      place("unit", dest);
      if (strip !== ".") checkRelative(`${dest}: strip prefix`, strip);
      units.push({ dest, digest: checkDigest(digest), archive, strip });
    } else if (kind === "link" && fields.length === 2) {
      const [at, target] = fields as [string, string];
      place("link", at);
      if (target === "" || target.startsWith("/"))
        fail(`link ${at} -> '${target}' is not a relative target`);
      links.push({ at, target });
    } else if (kind === "exec" && fields.length === 1) {
      checkRelative("executable", fields[0]!);
      if (executables.includes(fields[0]!))
        fail(`executable '${fields[0]}' is given twice`);
      executables.push(fields[0]!);
    } else {
      fail(`${file}: cannot read '${line}'`);
    }
  }
  if (mount === undefined) fail(`${file} names no mount`);
  // A unit or link inside another would be written into a clone or through a
  // link, so each entry's parents must be directories the tree makes.
  for (const at of placed) {
    const parts = at.split("/");
    for (let depth = 1; depth < parts.length; depth++) {
      if (placed.has(parts.slice(0, depth).join("/")))
        fail(`'${at}' sits inside another unit or link`);
    }
  }
  // A file outside every unit would be one the tree does not hold, or one a
  // link reaches, whose mode the tree must not change.
  for (const file of executables) {
    if (!units.some((unit) => file.startsWith(`${unit.dest}/`)))
      fail(`executable '${file}' is inside no unit`);
  }
  return { mount, units, links, executables };
}

// What the tree holds, independent of where any input sits.
export function treeKey(manifest: Manifest): string {
  const lines = [
    ...manifest.units.map(
      (unit) => `unit|${unit.dest}|${unit.digest}|${unit.strip}`,
    ),
    ...manifest.links.map((link) => `link|${link.at}|${link.target}`),
    ...manifest.executables.map((file) => `exec|${file}`),
  ].sort();
  return createHash("sha256").update(lines.join("\n")).digest("hex");
}

if (import.meta.main) {
  const argv = process.argv.slice(2);
  try {
    if (argv[0] !== "--resolve" || argv.length !== 3)
      fail("usage: archive_tree_manifest.ts --resolve TEMPLATE MANIFEST");
    resolve(argv[1]!, argv[2]!);
  } catch (error) {
    fs.writeSync(
      2,
      `archive_tree_manifest: error: ${error instanceof ManifestError ? error.message : ((error as Error).stack ?? String(error))}\n`,
    );
    process.exitCode = 1;
  }
}
