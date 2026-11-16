"""The Bun the linked tree suite runs the helper under."""

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")

_BUN = {
    "mac os x/aarch64": ("darwin-aarch64", "c240d7f0ccb90c263a849fcdb09b7ee62630d921348ed6aafda86b9d577bc510"),
    "mac os x/x86_64": ("darwin-x64", "50a3491f73abc2fe81673171a85762d3bc2cc1680ccecadd983de99adf77977e"),
    "linux/aarch64": ("linux-aarch64", "baa6bb36f51d6e58561e02bbea825038a9df454f673d6d776e8bbf86f8c7850e"),
    "linux/amd64": ("linux-x64-baseline", "e0446eb55995772eb9894bebae9a6be368d985a8b9271ee124486f9946eedd76"),
}

def _deps(ctx):
    platform = ctx.os.name + "/" + ctx.os.arch
    if platform not in _BUN:
        fail("Unsupported test host: " + platform)
    archive, sha256 = _BUN[platform]
    http_archive(
        name = "test_bun",
        # renovate: datasource=github-releases depName=oven-sh/bun extractVersion=^bun-v(?<version>.+)$
        urls = ["https://github.com/oven-sh/bun/releases/download/bun-v1.4.4/bun-" + archive + ".zip"],
        sha256 = sha256,
        strip_prefix = "bun-" + archive,
        build_file_content = 'exports_files(["bun"])',
    )

# The Bun downloaded depends on the host, so the lockfile keys this
# extension's result by both.
deps = module_extension(implementation = _deps, os_dependent = True, arch_dependent = True)
