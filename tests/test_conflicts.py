import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import main


class ConflictPolicyTests(unittest.TestCase):
    def test_push_race_detection(self) -> None:
        self.assertTrue(main.is_push_race(main.GitCommandError(["push"], "rejected non-fast-forward")))
        self.assertFalse(main.is_push_race(main.GitCommandError(["push"], "authentication failed")))

    def test_rebase_conflict_detection(self) -> None:
        self.assertTrue(main.is_rebase_conflict(main.GitCommandError(["pull"], "CONFLICT (content): Merge conflict")))
        self.assertFalse(main.is_rebase_conflict(main.GitCommandError(["pull"], "Could not resolve host")))

    def test_stale_manual_save_preserves_local_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sources.md"
            path.write_text("current\n", encoding="utf-8")
            with (
                patch.object(main, "sync_repo"),
                patch.object(main, "document_path", return_value=path),
                patch.object(main, "document_version", return_value="current-version"),
                patch.object(main, "commit_and_push") as commit,
            ):
                with self.assertRaises(HTTPException) as raised:
                    main.save_document("sources", "draft", "loaded-version")

            self.assertEqual(raised.exception.status_code, 409)
            self.assertIn("draft is still in the editor", raised.exception.detail)
            self.assertEqual(path.read_text(encoding="utf-8"), "current\n")
            commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
