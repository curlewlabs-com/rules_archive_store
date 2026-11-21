# Release preparation and publication

A version-specific notice under `docs/release-notes/<version>.md` opens the
generated notes for that release. Record a store namespace change there so
operators can budget for the first expansions after upgrade.

A release is a source-only archive of a reviewed commit: the committed module,
its tests, docs, examples and license, without Git metadata, untracked files or
build output.

## Which releases need approval

A patch or minor release needs no approval: whoever cuts it follows the steps
below in order, on a `main` commit whose required checks have passed. A major
version, a 1.0, or a minor before 1.0 that changes an interface consumers use
needs the maintainer's explicit approval before its tag is pushed.

## Prepare a candidate

Commit the candidate and run from a clean worktree (Bazel's own lockfile
refresh is allowed and left out of the archive):

```sh
python3 tools/prepare_release.py > /tmp/rules_archive_store-release-notes.md
```

The helper reads the module's name and version from Bazel, packages the
committed source with ownership, times and gzip metadata normalized, so the
archive's bytes depend on the commit alone, and runs the independent consumer
([examples/consumer](../examples/consumer)) against the extracted archive
rather than the checkout. Its files go under `dist/`: the versioned archive,
`SHA256SUMS`, and `source.json` with the commit, the archive's integrity and its
strip prefix. The notes on stdout carry a `bazel_dep` and `archive_override` for
the release URL, which works only once the release is published.

An argument such as `v0.1.0` must match the module's version. CI runs the same
preparation on every platform it tests. A green candidate is evidence for
review, not permission to publish.

## Create the draft

Push a fixed `vMAJOR.MINOR.PATCH` tag at the reviewed commit on `main`, with
approval first where the release needs it. Never move or replace a fixed tag.
The
[release workflow](../.github/workflows/release.yaml) checks that the commit is
on `main`, runs CI, and calls the pinned
[rules-template release workflow](https://github.com/bazel-contrib/.github/blob/v7.7.0/.github/workflows/release_ruleset.yaml),
which prepares the archive again through `.github/workflows/release_prep.sh`,
adds provenance attestations, and uploads the files to a draft release. A failed
workflow is investigated against that tag; source is never repaired by moving
it.

## Publish after review

Download the draft's assets, check the archive against `SHA256SUMS` and the
reviewed candidate, and confirm the notes' URL, integrity, strip prefix and
version match the asset. Verify the archive's provenance against the signing
workflow and the tag:

```sh
gh attestation verify rules_archive_store-X.Y.Z.tar.gz -R curlewlabs-com/rules_archive_store \
  --signer-workflow bazel-contrib/.github/.github/workflows/release_ruleset.yaml \
  --source-ref refs/tags/vX.Y.Z
```

Then publish the draft. Immutable releases and fixed-tag protection keep
published bytes from being rewritten; a published defect is fixed with a new
version.
