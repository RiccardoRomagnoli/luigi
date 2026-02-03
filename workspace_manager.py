import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Set


def _run(cmd: List[str], *, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def is_git_repo(path: str) -> bool:
    result = _run(["git", "rev-parse", "--is-inside-work-tree"], cwd=path)
    return result.returncode == 0 and result.stdout.strip() == "true"


def has_git_commit(path: str) -> bool:
    # A repo with no commits has no valid HEAD.
    result = _run(["git", "rev-parse", "--verify", "HEAD"], cwd=path)
    return result.returncode == 0


def _default_copy_ignore_patterns(extra: Optional[List[str]] = None) -> List[str]:
    patterns = [
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".DS_Store",
        "logs",
    ]
    if extra:
        patterns.extend(extra)
    return patterns


@dataclass
class Workspace:
    """A workspace where Claude Code will make changes."""

    repo_path: str
    path: str
    strategy: str  # "worktree" | "copy" | "in_place"
    run_dir: str
    baseline_path: Optional[str] = None
    branch_name: Optional[str] = None

    def get_diff(self) -> str:
        """Return a unified diff of changes made in the workspace."""
        # Prefer git diff when possible.
        if self.strategy in ("worktree", "in_place") and is_git_repo(self.path):
            result = _run(["git", "diff"], cwd=self.path)
            return (result.stdout or "").strip()

        # Snapshot-based diff (works without a git repo).
        if not self.baseline_path:
            return ""

        # Use git diff --no-index if git is available (best formatting).
        git_check = _run(["git", "--version"])
        if git_check.returncode == 0:
            result = _run(["git", "diff", "--no-index", "--", self.baseline_path, self.path])
            # git diff returns exit code 1 when there are diffs; that's not an error here.
            return (result.stdout or "").strip()

        return ""

    def cleanup(self) -> None:
        """Clean up any temporary workspace artifacts created for this run."""
        if self.strategy == "worktree":
            # Remove the worktree directory and its admin entry.
            # Use --force because worktrees may have uncommitted changes.
            _run(["git", "worktree", "remove", "--force", self.path], cwd=self.repo_path)
        # Always remove run_dir for non-worktree strategies (and worktree artifacts within it).
        if os.path.isdir(self.run_dir):
            shutil.rmtree(self.run_dir, ignore_errors=True)

    def apply_to_repo(self) -> None:
        """Apply changes back to the original repo path.

        This is only relevant for the "copy" strategy; other strategies operate directly
        in the target repo/worktree.
        """
        if self.strategy != "copy":
            return
        if not self.baseline_path:
            raise RuntimeError("Cannot apply copy-workspace changes without a baseline snapshot.")

        _sync_dir(src=self.path, dst=self.repo_path, baseline=self.baseline_path)

    def commit_changes(self, message: str) -> Optional[str]:
        """Commit changes in a git workspace, returning the new commit SHA if any."""
        if not is_git_repo(self.path):
            return None

        status = _run(["git", "status", "--porcelain"], cwd=self.path)
        if status.returncode != 0:
            raise RuntimeError(f"git status failed: {status.stderr.strip()}")
        if not status.stdout.strip():
            return None

        add_res = _run(["git", "add", "."], cwd=self.path)
        if add_res.returncode != 0:
            raise RuntimeError(f"git add failed: {add_res.stderr.strip()}")

        commit_res = _run(["git", "commit", "-m", message], cwd=self.path)
        if commit_res.returncode != 0:
            raise RuntimeError(f"git commit failed: {commit_res.stderr.strip()}")

        head = _run(["git", "rev-parse", "HEAD"], cwd=self.path)
        if head.returncode != 0:
            raise RuntimeError(f"git rev-parse HEAD failed: {head.stderr.strip()}")
        return head.stdout.strip() or None


def _iter_files(root: str) -> Set[str]:
    files: Set[str] = set()
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            abs_path = os.path.join(dirpath, name)
            rel = os.path.relpath(abs_path, root)
            files.add(rel)
    return files


def _sync_dir(*, src: str, dst: str, baseline: str) -> None:
    """Sync `src` directory into `dst`, including deletions relative to `baseline`."""
    src = os.path.abspath(src)
    dst = os.path.abspath(dst)
    baseline = os.path.abspath(baseline)

    src_files = _iter_files(src)
    baseline_files = _iter_files(baseline)

    # Copy/update files from src -> dst
    for rel in sorted(src_files):
        src_file = os.path.join(src, rel)
        dst_file = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(dst_file), exist_ok=True)
        shutil.copy2(src_file, dst_file)

    # Delete files that existed in baseline but are missing from src
    deleted = baseline_files - src_files
    for rel in sorted(deleted):
        dst_file = os.path.join(dst, rel)
        if os.path.exists(dst_file) and os.path.isfile(dst_file):
            os.remove(dst_file)

    # Best-effort: remove empty directories (walk bottom-up)
    for dirpath, dirnames, filenames in os.walk(dst, topdown=False):
        if os.path.abspath(dirpath) == dst:
            continue
        if not dirnames and not filenames:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass


class WorkspaceManager:
    """Creates isolated workspaces (git worktree when possible, otherwise snapshots)."""

    def __init__(self, base_dir: str):
        self.base_dir = os.path.abspath(base_dir)
        os.makedirs(self.base_dir, exist_ok=True)

    def create(
        self,
        *,
        repo_path: str,
        run_id: str,
        strategy: str = "auto",
        use_git_worktree: bool = True,
        copy_ignore_patterns: Optional[List[str]] = None,
    ) -> Workspace:
        repo_path = os.path.abspath(repo_path)
        os.makedirs(repo_path, exist_ok=True)
        run_dir = os.path.join(self.base_dir, run_id)
        os.makedirs(run_dir, exist_ok=True)

        ignore_patterns = _default_copy_ignore_patterns(copy_ignore_patterns)
        # If the workspace base dir lives inside the repo, exclude its folder name to avoid recursion.
        if os.path.commonpath([repo_path, self.base_dir]) == repo_path:
            ignore_patterns.append(os.path.relpath(self.base_dir, repo_path).split(os.sep)[0])

        if strategy == "auto":
            if use_git_worktree and is_git_repo(repo_path) and has_git_commit(repo_path):
                strategy = "worktree"
            else:
                strategy = "copy"

        if strategy == "worktree":
            if not is_git_repo(repo_path) or not has_git_commit(repo_path):
                raise RuntimeError("Requested git worktree strategy but repo is not a git repo with commits.")

            worktree_path = os.path.join(run_dir, "worktree")
            if os.path.exists(worktree_path):
                shutil.rmtree(worktree_path, ignore_errors=True)

            branch_name = f"orchestrator/{run_id}"
            result = _run(["git", "worktree", "add", "-b", branch_name, worktree_path], cwd=repo_path)
            if result.returncode != 0:
                raise RuntimeError(f"Failed to create git worktree: {result.stderr.strip()}")

            return Workspace(
                repo_path=repo_path,
                path=worktree_path,
                strategy="worktree",
                run_dir=run_dir,
                baseline_path=None,
                branch_name=branch_name,
            )

        if strategy == "copy":
            baseline_path = os.path.join(run_dir, "baseline")
            workspace_path = os.path.join(run_dir, "workspace")
            if os.path.exists(baseline_path):
                shutil.rmtree(baseline_path, ignore_errors=True)
            if os.path.exists(workspace_path):
                shutil.rmtree(workspace_path, ignore_errors=True)

            shutil.copytree(
                repo_path,
                baseline_path,
                ignore=shutil.ignore_patterns(*ignore_patterns),
                dirs_exist_ok=False,
            )
            shutil.copytree(baseline_path, workspace_path, dirs_exist_ok=False)

            return Workspace(
                repo_path=repo_path,
                path=workspace_path,
                strategy="copy",
                run_dir=run_dir,
                baseline_path=baseline_path,
            )

        if strategy == "in_place":
            baseline_path = os.path.join(run_dir, "baseline")
            if os.path.exists(baseline_path):
                shutil.rmtree(baseline_path, ignore_errors=True)
            shutil.copytree(
                repo_path,
                baseline_path,
                ignore=shutil.ignore_patterns(*ignore_patterns),
                dirs_exist_ok=False,
            )
            return Workspace(
                repo_path=repo_path,
                path=repo_path,
                strategy="in_place",
                run_dir=run_dir,
                baseline_path=baseline_path,
            )

        raise ValueError(f"Unknown workspace strategy: {strategy}")

