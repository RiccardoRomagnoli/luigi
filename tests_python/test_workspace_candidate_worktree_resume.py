import os
import subprocess
import tempfile
import unittest

from workspace_manager import WorkspaceManager


class WorkspaceCandidateWorktreeResumeTest(unittest.TestCase):
    def test_create_candidate_worktree_reuses_existing_branch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="luigi-wt-") as tmp:
            repo_path = os.path.join(tmp, "repo")
            os.makedirs(repo_path, exist_ok=True)

            subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True, text=True)
            # Minimal commit so worktrees are allowed.
            with open(os.path.join(repo_path, "README.md"), "w", encoding="utf-8") as f:
                f.write("hello\n")
            subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True, text=True)
            subprocess.run(
                ["git", "commit", "-m", "init"],
                cwd=repo_path,
                check=True,
                capture_output=True,
                text=True,
            )

            mgr = WorkspaceManager(os.path.join(tmp, "workspaces"))
            ws1 = mgr.create_candidate(
                repo_path=repo_path,
                run_id="run-1",
                iteration=1,
                candidate_id="cand-1",
                strategy="worktree",
                use_git_worktree=True,
            )
            self.assertTrue(os.path.isdir(ws1.path))

            # Second call should reuse (not fail due to existing branch).
            ws2 = mgr.create_candidate(
                repo_path=repo_path,
                run_id="run-1",
                iteration=1,
                candidate_id="cand-1",
                strategy="worktree",
                use_git_worktree=True,
            )
            self.assertEqual(ws2.path, ws1.path)


if __name__ == "__main__":
    unittest.main()

