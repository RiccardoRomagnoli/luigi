
import subprocess
import os

class GitUtils:
    """Utilities for managing git worktrees."""

    def __init__(self, base_dir):
        """Initializes the GitUtils."""
        self.base_dir = base_dir
        if not os.path.exists(self.base_dir):
            os.makedirs(self.base_dir)

    def create_worktree(self, worktree_name):
        """Creates a new git worktree."""
        worktree_path = os.path.join(self.base_dir, worktree_name)
        print(f"Creating git worktree at: {worktree_path}")
        # In a real scenario, you would initialize a git repo if it doesn't exist
        if not os.path.exists(os.path.join(self.base_dir, ".git")):
            subprocess.run(["git", "init"], cwd=self.base_dir, check=True)
        subprocess.run(["git", "worktree", "add", worktree_path], cwd=self.base_dir, check=True)
        return worktree_path

    def remove_worktree(self, worktree_path):
        """Removes a git worktree."""
        print(f"Removing git worktree at: {worktree_path}")
        subprocess.run(["git", "worktree", "remove", worktree_path], cwd=self.base_dir, check=True)

    def commit_changes(self, message):
        """Commits changes in the current worktree."""
        print(f"Committing changes with message: {message}")
        subprocess.run(["git", "add", "."], check=True)
        subprocess.run(["git", "commit", "-m", message], check=True)
