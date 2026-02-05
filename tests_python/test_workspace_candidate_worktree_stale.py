import os
import shutil
import subprocess
import tempfile
import unittest

from workspace_manager import WorkspaceManager, is_git_repo


class WorkspaceCandidateWorktreeStaleTest(unittest.TestCase):
    def test_create_candidate_cleans_stale_worktree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="luigi-wt-") as tmp:
            repo_path = os.path.join(tmp, "repo")
            os.makedirs(repo_path, exist_ok=True)

            subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True, text=True)
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
            self.assertTrue(is_git_repo(ws1.path))

            # Simulate crash: remove worktree directory without git worktree remove.
            shutil.rmtree(ws1.path, ignore_errors=True)
            self.assertFalse(os.path.exists(ws1.path))

            ws2 = mgr.create_candidate(
                repo_path=repo_path,
                run_id="run-1",
                iteration=1,
                candidate_id="cand-1",
                strategy="worktree",
                use_git_worktree=True,
            )
            self.assertTrue(os.path.isdir(ws2.path))
            self.assertTrue(is_git_repo(ws2.path))


if __name__ == "__main__":
    unittest.main()

