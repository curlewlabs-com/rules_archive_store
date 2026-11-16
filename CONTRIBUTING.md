# Contributing

Use the Bazel release in `.bazelversion` and Git. The test suite downloads its
own Bun and Python. Keep changes on a branch and open a pull request with the
behavior change and its verification.

## Maintainership and review

Curlew Labs maintains this project. Jeffrey Wall
([jeffwall-curlewlabs](https://github.com/jeffwall-curlewlabs)) reviews changes
and approves releases. Use issues for reproducible defects and proposals; use
[private vulnerability reporting](SECURITY.md) for security concerns.

Changes land through pull requests with passing CI and maintainer review;
[toolchain updates](#toolchain-updates) merge on passing CI alone. A
change to the store's protocol changes the helper's digest, and with it the
namespace its expansions go in, so a store never mixes two protocols; keep
[docs/store.md](docs/store.md) and the code saying the same thing in the same
change. Fixed version tags and published release artifacts are never replaced.

## Local checks

Install `pre-commit==4.3.0`, then run:

```sh
pre-commit install
pre-commit run --all-files
bazel test //...
```

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

## Releases

Follow [releases](docs/releases.md).
