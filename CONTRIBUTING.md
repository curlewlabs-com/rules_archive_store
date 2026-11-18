# Contributing

Thanks for your interest in improving `rules_archive_store`. The bar for a
change is whether every action still reads exactly the tree its archive's
digest names, from an expansion each executor instance publishes once and never
evicts while a reader holds it. Bug fixes, sharper refusals of archive members
the conversion cannot place safely, support for another executor, and clearer
docs are all welcome.

Use the Bazel release in `.bazelversion` and Git. The test suite downloads its
own Bun and Python. Keep changes on a branch and open a pull request with the
behavior change and its verification.

## Before a large change

Propose it in an issue before writing it. The guarantees rest on rules that are
easy to weaken by accident, and [docs/store.md](docs/store.md) records them: the
conversion's member and link checks, the publication protocol, holds and
eviction, and what keeps a store readable by an earlier release's evictor.
[docs/executor.md](docs/executor.md) records what an executor must provide. A
short discussion up front saves rework.

## Maintainership and review

Curlew Labs maintains this project. Jeffrey Wall
([jeffwall-curlewlabs](https://github.com/jeffwall-curlewlabs)) reviews changes
and approves the releases that
[need approval](docs/releases.md#which-releases-need-approval). Use issues for
reproducible defects and proposals; use
[private vulnerability reporting](SECURITY.md) for security concerns.

Changes land through pull requests with passing CI and maintainer review;
[toolchain updates](#toolchain-updates) merge on passing CI alone. A
change to the store's protocol changes the helper's digest, and with it the
namespace its expansions go in, so a store never mixes two protocols; keep
[docs/store.md](docs/store.md) and the code saying the same thing in the same
change. Fixed version tags and published release artifacts are never replaced.

## Local checks

```sh
python3 -m pip install -r requirements-dev.txt
mypy
bun install --frozen-lockfile
bun run --bun typecheck
bazel test //...
```

mypy needs Python 3.14 and the type check needs Bun; `requirements-dev.txt`
and `package.json` pin what they install. Formatting and linting (ruff,
buildifier, shfmt, prettier, shellcheck, actionlint) run in CI's `baseline`
job, from [ci-baseline](https://github.com/curlewlabs-com/ci-baseline).

The suites exercise the real helpers against real stores in temporary
directories. On macOS they also build linked trees with `clonefile(2)` and ask
the volume which names it merges, which needs a case-insensitive APFS volume;
on Linux those suites skip. CI runs both.

## Toolchain updates

[Renovate](https://docs.renovatebot.com/) proposes each new Bun release, each
new minor and patch release of Bazel, and each new release of the workflow
actions, once it has aged, and merges the pull request when CI passes. A new
Bazel major is moved by hand. The `# renovate:` comment above a pin tells
Renovate what the literal below is. After moving a version it runs

```sh
python3 tools/refresh_toolchain.py
```

which writes each host's Bun archive digest from the release's
`SHASUMS256.txt` and the versions in the README. Run the same command after
moving a pin by hand. `MODULE.bazel.lock` is not part of it: Bazel rewrites the
host's entries on its next run.

The type check's pins in `package.json` move with `bun add --dev --exact`,
which rewrites `bun.lock`; keep `@types/bun` at the Bun release
`tests/deps.bzl` pins.

## Releases

Follow [releases](docs/releases.md).

## License

By submitting a contribution for inclusion in this project, you agree that it
is licensed under the MIT and Apache-2.0 licenses, as the README's License
section describes, without any additional terms or conditions.
