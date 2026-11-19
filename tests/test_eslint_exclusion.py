"""The eslint exclusion for the two helpers that name the store namespace
(.ci-baseline/eslint.json) must not outlive the content it was written for.

A path-only exclusion keeps matching after either file is edited, which would
leave the edited helper unchecked. This fails as soon as the namespace the two
files name moves, so the change that moves it must also fix their lint
findings and remove the exclusion, then record the new namespace here.
"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

_HELPER = Path(__file__).parent.parent / "archive_store" / "archive_tree.ts"
_MANIFEST = _HELPER.with_name("archive_tree_manifest.ts")
# The prefix archive_tree.ts names its namespace by: the digest of both files.
_EXCLUDED_AT = "058472967efeb381"


class EslintExclusionTest(unittest.TestCase):
    def test_exclusion_is_removed_when_the_namespace_moves(self) -> None:
        namespace = hashlib.sha256(
            _HELPER.read_bytes() + _MANIFEST.read_bytes()
        ).hexdigest()[:16]
        self.assertEqual(
            namespace,
            _EXCLUDED_AT,
            "archive_tree.ts or archive_tree_manifest.ts changed: fix their"
            " eslint findings, remove their entries from .ci-baseline/eslint.json"
            " and the README's Known issues, and delete this test",
        )


if __name__ == "__main__":
    unittest.main()
