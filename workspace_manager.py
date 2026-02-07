import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Set


def _validate_dir_name(value: str, *, label: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise ValueError(f"{label} must be a non-empty string.")
    if value in (".", ".."):
        raise ValueError(f"{label} must not be '.' or '..'.")
    seps = [os.sep]
    if os.altsep:
        seps.append(os.altsep)
    if any(sep and sep in value for sep in seps):
        raise ValueError(f"{label} must not contain path separators.")
    if "\x00" in value:
        raise ValueError(f"{label} must not contain NUL bytes.")
    return value


def _safe_join(root: str, *parts: str) -> str:
    root_abs = os.path.abspath(root)
    path = os.path.abspath(os.path.join(root_abs, *parts))
    if os.path.commonpath([root_abs, path]) != root_abs:
        raise RuntimeError(f"Refusing to create path outside base directory: {path}")
    return path


_COMPONENT_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_component(value: str, *, max_len: int = 80) -> str:
    raw = str(value or "")
    raw = raw.replace("..", "_")
    raw = raw.replace(os.sep, "_")
    if os.altsep:
        raw = raw.replace(os.altsep, "_")

    cleaned = _COMPONENT_SAFE_RE.sub("_", raw).strip("._-")
    if not cleaned:
        cleaned = "x"
    if len(cleaned) > max_len:
        digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
        cleaned = cleaned[: max(1, max_len - 13)] + "_" + digest
    return cleaned


def _sanitize_branch_prefix(value: str) -> str:
    prefix = _sanitize_component(value, max_len=24)
    return prefix or "luigi"


def _short_id(value: str, *, length: int) -> str:
    length = max(4, min(int(length or 8), 24))
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", str(value))
    if not cleaned:
        cleaned = _sanitize_component(str(value), max_len=length)
    return cleaned[:length]


def _short_hash(value: str, *, length: int) -> str:
    length = max(4, min(int(length or 6), 16))
    digest = hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()
    return digest[:length]


def _safe_dest_path(dst_root: str, rel_path: str, *, allow_symlink_file: bool) -> str:
    """Resolve `rel_path` under `dst_root` and refuse symlink traversal.

    This protects the "copy" workspace apply step from overwriting files outside the repo
    when the destination contains symlinked directories or files.
    """
    dst_root_abs = os.path.abspath(dst_root)
    rel_path = str(rel_path or "")
    # Avoid absolute rel paths causing `os.path.join` to discard dst_root.
    rel_path = rel_path.lstrip(os.sep)
    if os.altsep:
        rel_path = rel_path.lstrip(os.altsep)

    dst_path = os.path.abspath(os.path.join(dst_root_abs, rel_path))
    if os.path.commonpath([dst_root_abs, dst_path]) != dst_root_abs:
        raise RuntimeError(f"Refusing to write outside destination root: {dst_path}")

    rel_parts = os.path.relpath(dst_path, dst_root_abs).split(os.sep)
    cur = dst_root_abs
    # Forbid symlinked directories along the path.
    for part in rel_parts[:-1]:
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            raise RuntimeError(f"Refusing to write through symlinked destination directory: {cur}")

    if not allow_symlink_file and os.path.islink(dst_path):
        raise RuntimeError(f"Refusing to overwrite symlinked destination file: {dst_path}")

    return dst_path


def _run(cmd: List[str], *, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)

def _git_branch_exists(repo_path: str, branch_name: str) -> bool:
    # branch_name should be a ref name like "orchestrator/<...>"
    result = _run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"], cwd=repo_path)
    return result.returncode == 0


def _find_worktree_for_branch(repo_path: str, branch_name: str) -> Optional[str]:
    """Return the worktree path where branch is checked out, if any."""
    result = _run(["git", "worktree", "list", "--porcelain"], cwd=repo_path)
    if result.returncode != 0:
        return None
    path: Optional[str] = None
    branch_ref = f"refs/heads/{branch_name}"
    for line in (result.stdout or "").splitlines():
        if line.startswith("worktree "):
            path = line.split(" ", 1)[1].strip()
            continue
        if line.startswith("branch ") and path:
            ref = line.split(" ", 1)[1].strip()
            if ref == branch_ref:
                return path
            path = None
    return None


def _is_registered_worktree(repo_path: str, worktree_path: str) -> bool:
    result = _run(["git", "worktree", "list", "--porcelain"], cwd=repo_path)
    if result.returncode != 0:
        return False
    for line in (result.stdout or "").splitlines():
        if line.startswith("worktree ") and line.split(" ", 1)[1].strip() == worktree_path:
            return True
    return False


def _cleanup_stale_worktree(repo_path: str, worktree_path: str) -> bool:
    """Remove a registered worktree whose path no longer exists."""
    if os.path.exists(worktree_path):
        return False
    if not _is_registered_worktree(repo_path, worktree_path):
        return False
    _run(["git", "worktree", "remove", "--force", worktree_path], cwd=repo_path)
    _run(["git", "worktree", "prune"], cwd=repo_path)
    return True


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
        """Clean up any temporary workspace artifacts created for this run.

        Multi-agent runs can create multiple git worktrees under the same `run_dir`.
        If we delete `run_dir` without unregistering those worktrees first, git will
        keep stale worktree entries (and future runs may "resurrect" them).
        """

        # Best-effort: unregister any registered git worktrees living under run_dir.
        try:
            run_dir_abs = os.path.abspath(self.run_dir)
            result = _run(["git", "worktree", "list", "--porcelain"], cwd=self.repo_path)
            if result.returncode == 0:
                worktree_paths: List[str] = []
                for line in (result.stdout or "").splitlines():
                    if not line.startswith("worktree "):
                        continue
                    wt_path = line.split(" ", 1)[1].strip()
                    if wt_path:
                        worktree_paths.append(wt_path)

                nested: List[str] = []
                for wt_path in worktree_paths:
                    wt_abs = os.path.abspath(wt_path)
                    try:
                        if os.path.commonpath([run_dir_abs, wt_abs]) == run_dir_abs:
                            nested.append(wt_abs)
                    except ValueError:
                        # Different drives (Windows) or invalid paths.
                        continue

                # Remove deepest paths first (avoids parent/child ordering issues).
                nested.sort(key=lambda p: len(p.split(os.sep)), reverse=True)
                for wt_abs in nested:
                    _run(["git", "worktree", "remove", "--force", wt_abs], cwd=self.repo_path)
                if nested:
                    _run(["git", "worktree", "prune"], cwd=self.repo_path)
        except Exception:
            pass

        if self.strategy == "worktree":
            # Remove the worktree directory and its admin entry.
            # Use --force because worktrees may have uncommitted changes.
            _run(["git", "worktree", "remove", "--force", self.path], cwd=self.repo_path)

        # Always remove run_dir for non-worktree strategies (and any remaining artifacts within it).
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
        dst_file = _safe_dest_path(dst, rel, allow_symlink_file=False)
        os.makedirs(os.path.dirname(dst_file), exist_ok=True)
        if os.path.islink(src_file):
            raise RuntimeError(f"Refusing to copy symlinked file into repo: {src_file}")
        shutil.copy2(src_file, dst_file)

    # Delete files that existed in baseline but are missing from src
    deleted = baseline_files - src_files
    for rel in sorted(deleted):
        dst_file = _safe_dest_path(dst, rel, allow_symlink_file=True)
        if os.path.lexists(dst_file) and (os.path.isfile(dst_file) or os.path.islink(dst_file)):
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
        branch_prefix: str = "luigi",
        branch_name_length: int = 8,
    ) -> Workspace:
        repo_path = os.path.abspath(repo_path)
        os.makedirs(repo_path, exist_ok=True)
        run_id = _validate_dir_name(run_id, label="run_id")
        run_dir = _safe_join(self.base_dir, run_id)
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
            prefix = _sanitize_branch_prefix(branch_prefix)
            short_run = _short_id(run_id, length=branch_name_length)
            branch_name = f"{prefix}/{short_run}"
            if os.path.isdir(worktree_path) and is_git_repo(worktree_path):
                return Workspace(
                    repo_path=repo_path,
                    path=worktree_path,
                    strategy="worktree",
                    run_dir=run_dir,
                    baseline_path=None,
                    branch_name=branch_name,
                )

            existing_path = _find_worktree_for_branch(repo_path, branch_name)
            if existing_path:
                if os.path.isdir(existing_path) and is_git_repo(existing_path):
                    return Workspace(
                        repo_path=repo_path,
                        path=existing_path,
                        strategy="worktree",
                        run_dir=run_dir,
                        baseline_path=None,
                        branch_name=branch_name,
                    )
                _cleanup_stale_worktree(repo_path, existing_path)

            force_add = _cleanup_stale_worktree(repo_path, worktree_path)
            if _git_branch_exists(repo_path, branch_name):
                cmd = ["git", "worktree", "add"]
                if force_add:
                    cmd.append("-f")
                cmd.extend([worktree_path, branch_name])
            else:
                cmd = ["git", "worktree", "add"]
                if force_add:
                    cmd.append("-f")
                cmd.extend(["-b", branch_name, worktree_path])
            result = _run(cmd, cwd=repo_path)
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
            # Idempotent resume: if both baseline + workspace exist, reuse them.
            if os.path.isdir(baseline_path) and os.path.isdir(workspace_path):
                return Workspace(
                    repo_path=repo_path,
                    path=workspace_path,
                    strategy="copy",
                    run_dir=run_dir,
                    baseline_path=baseline_path,
                )
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

    def create_candidate(
        self,
        *,
        repo_path: str,
        source_path: Optional[str] = None,
        run_id: str,
        iteration: int,
        candidate_id: str,
        strategy: str = "auto",
        use_git_worktree: bool = True,
        copy_ignore_patterns: Optional[List[str]] = None,
        branch_prefix: str = "luigi",
        branch_name_length: int = 8,
        branch_suffix_length: int = 6,
    ) -> Workspace:
        repo_path = os.path.abspath(repo_path)
        source_root = os.path.abspath(source_path or repo_path)
        os.makedirs(repo_path, exist_ok=True)
        run_id = _validate_dir_name(run_id, label="run_id")
        candidate_slug = _sanitize_component(candidate_id, max_len=80)
        run_dir = _safe_join(self.base_dir, run_id, f"iter_{iteration}", f"cand_{candidate_slug}")
        os.makedirs(run_dir, exist_ok=True)

        ignore_patterns = _default_copy_ignore_patterns(copy_ignore_patterns)
        if os.path.commonpath([source_root, self.base_dir]) == source_root:
            ignore_patterns.append(os.path.relpath(self.base_dir, source_root).split(os.sep)[0])

        if strategy == "auto":
            if use_git_worktree and is_git_repo(repo_path) and has_git_commit(repo_path):
                strategy = "worktree"
            else:
                strategy = "copy"

        if strategy == "worktree":
            if not is_git_repo(repo_path) or not has_git_commit(repo_path):
                raise RuntimeError("Requested git worktree strategy but repo is not a git repo with commits.")
            worktree_path = os.path.join(run_dir, "worktree")
            prefix = _sanitize_branch_prefix(branch_prefix)
            short_run = _short_id(run_id, length=branch_name_length)
            short_suffix = _short_hash(candidate_id, length=branch_suffix_length)
            branch_name = f"{prefix}/{short_run}-i{iteration}-{short_suffix}"
            # Idempotent resume: if worktree path already exists and is a git worktree, reuse it.
            if os.path.isdir(worktree_path) and is_git_repo(worktree_path):
                return Workspace(
                    repo_path=repo_path,
                    path=worktree_path,
                    strategy="worktree",
                    run_dir=run_dir,
                    baseline_path=None,
                    branch_name=branch_name,
                )

            # If the branch is already checked out somewhere (common on crash-resume), reuse that worktree.
            existing_path = _find_worktree_for_branch(repo_path, branch_name)
            if existing_path:
                if os.path.isdir(existing_path) and is_git_repo(existing_path):
                    return Workspace(
                        repo_path=repo_path,
                        path=existing_path,
                        strategy="worktree",
                        run_dir=run_dir,
                        baseline_path=None,
                        branch_name=branch_name,
                    )
                _cleanup_stale_worktree(repo_path, existing_path)

            # Create or attach the worktree.
            force_add = _cleanup_stale_worktree(repo_path, worktree_path)
            if _git_branch_exists(repo_path, branch_name):
                cmd = ["git", "worktree", "add"]
                if force_add:
                    cmd.append("-f")
                cmd.extend([worktree_path, branch_name])
            else:
                cmd = ["git", "worktree", "add"]
                if force_add:
                    cmd.append("-f")
                cmd.extend(["-b", branch_name, worktree_path])
            result = _run(cmd, cwd=repo_path)
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
            # Idempotent resume: if both baseline + workspace exist, reuse them.
            if os.path.isdir(baseline_path) and os.path.isdir(workspace_path):
                return Workspace(
                    repo_path=repo_path,
                    path=workspace_path,
                    strategy="copy",
                    run_dir=run_dir,
                    baseline_path=baseline_path,
                )
            if os.path.exists(baseline_path):
                shutil.rmtree(baseline_path, ignore_errors=True)
            if os.path.exists(workspace_path):
                shutil.rmtree(workspace_path, ignore_errors=True)
            shutil.copytree(
                source_root,
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
            # Idempotent resume: reuse baseline snapshot if it already exists.
            if os.path.isdir(baseline_path):
                return Workspace(
                    repo_path=repo_path,
                    path=repo_path,
                    strategy="in_place",
                    run_dir=run_dir,
                    baseline_path=baseline_path,
                )
            if os.path.exists(baseline_path):
                shutil.rmtree(baseline_path, ignore_errors=True)
            shutil.copytree(
                source_root,
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

    def resume_candidate(
        self,
        *,
        repo_path: str,
        run_id: str,
        iteration: int,
        candidate_id: str,
        workspace_path: Optional[str],
        workspace_strategy: Optional[str],
    ) -> Optional[Workspace]:
        """Rehydrate a candidate workspace from persisted state (best-effort)."""
        if not workspace_strategy:
            return None
        repo_path = os.path.abspath(repo_path)
        strategy = str(workspace_strategy)
        if strategy == "worktree":
            if workspace_path and os.path.isdir(workspace_path) and is_git_repo(workspace_path):
                run_dir = os.path.dirname(workspace_path)
                return Workspace(
                    repo_path=repo_path,
                    path=workspace_path,
                    strategy="worktree",
                    run_dir=run_dir,
                    baseline_path=None,
                    branch_name=None,
                )
            return None
        if strategy == "copy":
            if not workspace_path:
                return None
            run_dir = os.path.dirname(workspace_path)
            baseline_path = os.path.join(run_dir, "baseline")
            if os.path.isdir(workspace_path) and os.path.isdir(baseline_path):
                return Workspace(
                    repo_path=repo_path,
                    path=workspace_path,
                    strategy="copy",
                    run_dir=run_dir,
                    baseline_path=baseline_path,
                )
            return None
        if strategy == "in_place":
            candidate_slug = _sanitize_component(candidate_id, max_len=80)
            run_dir = _safe_join(self.base_dir, run_id, f"iter_{iteration}", f"cand_{candidate_slug}")
            baseline_path = os.path.join(run_dir, "baseline")
            if os.path.isdir(baseline_path):
                return Workspace(
                    repo_path=repo_path,
                    path=repo_path,
                    strategy="in_place",
                    run_dir=run_dir,
                    baseline_path=baseline_path,
                )
            return Workspace(
                repo_path=repo_path,
                path=repo_path,
                strategy="in_place",
                run_dir=run_dir,
                baseline_path=None,
            )
        return None
