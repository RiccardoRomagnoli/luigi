import os
import tempfile
import unittest

from workspace_manager import WorkspaceManager


class WorkspaceCandidateTest(unittest.TestCase):
    def test_create_candidate_copy(self) -> None:
        with tempfile.TemporaryDirectory(prefix="luigi-ws-") as tmp:
            repo_path = os.path.join(tmp, "repo")
            os.makedirs(repo_path, exist_ok=True)
            with open(os.path.join(repo_path, "file.txt"), "w") as f:
                f.write("hello")

            mgr = WorkspaceManager(os.path.join(tmp, "workspaces"))
            ws = mgr.create_candidate(
                repo_path=repo_path,
                run_id="run-1",
                iteration=1,
                candidate_id="cand-1",
                strategy="copy",
                use_git_worktree=False,
            )
            self.assertTrue(os.path.isdir(ws.path))
            self.assertTrue(os.path.isfile(os.path.join(ws.path, "file.txt")))

    def test_create_candidate_copy_uses_source_but_applies_to_repo(self) -> None:
        with tempfile.TemporaryDirectory(prefix="luigi-ws-") as tmp:
            repo_path = os.path.join(tmp, "repo")
            source_path = os.path.join(tmp, "source")
            os.makedirs(repo_path, exist_ok=True)
            os.makedirs(source_path, exist_ok=True)
            with open(os.path.join(repo_path, "file.txt"), "w") as f:
                f.write("from-repo")
            with open(os.path.join(source_path, "file.txt"), "w") as f:
                f.write("from-source")

            mgr = WorkspaceManager(os.path.join(tmp, "workspaces"))
            ws = mgr.create_candidate(
                repo_path=repo_path,
                source_path=source_path,
                run_id="run-1",
                iteration=1,
                candidate_id="cand-1",
                strategy="copy",
                use_git_worktree=False,
            )
            with open(os.path.join(ws.path, "file.txt"), "r") as f:
                self.assertEqual(f.read(), "from-source")

            with open(os.path.join(ws.path, "file.txt"), "w") as f:
                f.write("updated")
            ws.apply_to_repo()

            with open(os.path.join(repo_path, "file.txt"), "r") as f:
                self.assertEqual(f.read(), "updated")

    def test_apply_to_repo_refuses_destination_symlink(self) -> None:
        with tempfile.TemporaryDirectory(prefix="luigi-ws-") as tmp:
            outside_path = os.path.join(tmp, "outside.txt")
            with open(outside_path, "w") as f:
                f.write("outside")

            repo_path = os.path.join(tmp, "repo")
            os.makedirs(repo_path, exist_ok=True)

            link_path = os.path.join(repo_path, "victim.txt")
            try:
                os.symlink(outside_path, link_path)
            except (OSError, NotImplementedError):
                self.skipTest("Symlinks not supported on this platform.")

            mgr = WorkspaceManager(os.path.join(tmp, "workspaces"))
            ws = mgr.create(
                repo_path=repo_path,
                run_id="run-1",
                strategy="copy",
                use_git_worktree=False,
            )
            with open(os.path.join(ws.path, "victim.txt"), "w") as f:
                f.write("updated")

            with self.assertRaises(RuntimeError):
                ws.apply_to_repo()

            with open(outside_path, "r") as f:
                self.assertEqual(f.read(), "outside")


if __name__ == "__main__":
    unittest.main()

