import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main


class ConflictPolicyTests(unittest.TestCase):
    def test_unconfigured_console_does_not_fall_back_to_basic_auth(self) -> None:
        with (
            patch.object(main, "GOOGLE_CLIENT_ID", ""),
            patch.object(main, "GOOGLE_CLIENT_SECRET", ""),
            patch.object(main, "GOOGLE_ALLOWED_EMAIL", ""),
            patch.object(main, "SESSION_SECRET", ""),
        ):
            response = TestClient(main.app).get("/", follow_redirects=False)

        self.assertEqual(response.status_code, 503)
        self.assertIn("Google OAuth", response.text)

    def test_recent_commits_includes_changed_paths(self) -> None:
        output = "\x1eabcdef0123456789\x1f2026-08-01T12:00:00+00:00\x1fAcquire source\n" \
            "docs/research/sources.md\n" \
            "evidence/derived/site.txt\n"
        with patch.object(main, "run_git", return_value=output):
            commits = main.recent_commits()

        self.assertEqual(commits[0]["sha"], "abcdef0")
        self.assertEqual(commits[0]["subject"], "Acquire source")
        self.assertEqual(commits[0]["paths"], ["docs/research/sources.md", "evidence/derived/site.txt"])

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
