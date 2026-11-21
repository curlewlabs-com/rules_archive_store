"""The store namespaces must move only when someone means to move them.

Every consumer's expansions live under a namespace named by a digest: the
shell helper's own (archive_mount.sh), and the linked tree helper's, which is
the digest of archive_tree.ts and archive_tree_manifest.ts together. Editing
any of those files moves its namespace, and every consumer then rebuilds its
store cold. A shell fix once moved the namespace unnoticed, so this pins both
values, computed the way the helpers compute them.

When the failure is intended: update the pinned value in the same change and
say so first in the release notes.
"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

_STORE = Path(__file__).parent.parent / "archive_store"
# archive_mount.sh: sha256_of "$0" | cut -c1-16.
_MOUNT_NAMESPACE = "2cdfa042dbc99169"
# archive_tree.ts: sha256 over its own bytes then the manifest's, first 16 hex.
_TREE_NAMESPACE = "880dd4beaf2d58ed"

_MOVED = (
    "the store namespace moved: every consumer's store will rebuild cold. If that"
    " is intended, update the pinned value in this change and say so first in"
    " the release notes"
)


class NamespaceStabilityTest(unittest.TestCase):
    def test_the_shell_helpers_namespace_has_not_moved(self) -> None:
        namespace = hashlib.sha256(
            (_STORE / "archive_mount.sh").read_bytes()
        ).hexdigest()[:16]
        self.assertEqual(namespace, _MOUNT_NAMESPACE, _MOVED)

    def test_the_tree_helpers_namespace_has_not_moved(self) -> None:
        namespace = hashlib.sha256(
            (_STORE / "archive_tree.ts").read_bytes()
            + (_STORE / "archive_tree_manifest.ts").read_bytes()
        ).hexdigest()[:16]
        self.assertEqual(namespace, _TREE_NAMESPACE, _MOVED)


if __name__ == "__main__":
    unittest.main()
