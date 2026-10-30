"""The Bun the linked tree suite runs the helper under."""

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")

_BUN = {
    "mac os x/aarch64": ("darwin-aarch64", "82034e87c9d9b4398ea619aee2eed5d2a68c8157e9a6ae2d1052d84d533ccd8d"),
    "mac os x/x86_64": ("darwin-x64", "c1d90bf6140f20e572c473065dc6b37a4b036349b5e9e4133779cc642ad94323"),
    "linux/aarch64": ("linux-aarch64", "fa5ecb25cafa8e8f5c87a0f833719d46dd0af0a86c7837d806531212d55636d3"),
    "linux/amd64": ("linux-x64-baseline", "41201a8c5ee74a9dcbb1ce25a1104f1f929838b57a845aa78d98379b0ce7cde2"),
}

def _deps(ctx):
    platform = ctx.os.name + "/" + ctx.os.arch
    if platform not in _BUN:
        fail("Unsupported test host: " + platform)
    archive, sha256 = _BUN[platform]
    http_archive(
        name = "test_bun",
        urls = ["https://github.com/oven-sh/bun/releases/download/bun-v1.3.10/bun-" + archive + ".zip"],
        sha256 = sha256,
        strip_prefix = "bun-" + archive,
        build_file_content = 'exports_files(["bun"])',
    )

deps = module_extension(implementation = _deps)
