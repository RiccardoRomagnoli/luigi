import json
import os
import shutil
import subprocess
import tempfile
import unittest


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class MultiAgentExecutorFeedbackTest(unittest.TestCase):
    def _run_luigi(self, *, task: str, target_repo: str, config: dict) -> subprocess.CompletedProcess:
        config_path = os.path.join(os.path.dirname(target_repo), "config.mock.json")
        with open(config_path, "w") as f:
            json.dump(config, f)

        cmd = [
            "node",
            os.path.join(REPO_ROOT, "bin", "luigi.js"),
            task,
            "--repo",
            target_repo,
            "--config",
            config_path,
        ]
        env = os.environ.copy()
        env["LUIGI_PYTHON"] = env.get("LUIGI_PYTHON", "python3")
        return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, env=env)

    def test_claude_executor_needs_reviewer_is_routed(self) -> None:
        fixture_src = os.path.join(REPO_ROOT, "tests", "fixtures", "target-project")
        self.assertTrue(os.path.isdir(fixture_src))

        with tempfile.TemporaryDirectory(prefix="luigi-it-multi-feedback-claude-") as tmp:
            target_repo = os.path.join(tmp, "target-project")
            shutil.copytree(fixture_src, target_repo)

            config = {
                "codex": {
                    "command": ["node", os.path.join(REPO_ROOT, "tests", "mocks", "codex_mock.js")],
                    "model": "gpt-5.2-codex",
                    "reasoning_effort": "xhigh",
                    "sandbox": "read-only",
                    "approval_policy": "never",
                },
                "claude_code": {
                    "command": ["node", os.path.join(REPO_ROOT, "tests", "mocks", "claude_mock_needs_reviewer.js")],
                    "model": "opus",
                    "allowed_tools": ["Bash", "Read", "Edit", "Write", "Glob", "Grep"],
                    "max_turns": 1,
                },
                "orchestrator": {
                    "multi_agent": True,
                    "max_iterations": 1,
                    "max_claude_question_rounds": 3,
                    "working_dir": os.path.join(tmp, "workspaces"),
                    "logs_dir": os.path.join(tmp, "logs"),
                    "workspace_strategy": "copy",
                    "use_git_worktree": False,
                    "cleanup": "always",
                    "apply_changes_on_success": True,
                    "commit_on_approval": False,
                    "ui": {"enabled": False},
                },
                "testing": {"timeout_sec": 60, "install_if_missing": False},
            }

            result = self._run_luigi(
                task="Fix division by zero in divide()",
                target_repo=target_repo,
                config=config,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=f"Luigi failed.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}\n",
            )

            divide_path = os.path.join(target_repo, "src", "divide.js")
            with open(divide_path, "r") as f:
                content = f.read()
            self.assertIn('throw new Error("Division by zero")', content)

    def test_codex_executor_needs_reviewer_is_routed_to_claude_reviewer(self) -> None:
        fixture_src = os.path.join(REPO_ROOT, "tests", "fixtures", "target-project")
        self.assertTrue(os.path.isdir(fixture_src))

        with tempfile.TemporaryDirectory(prefix="luigi-it-multi-feedback-codex-") as tmp:
            target_repo = os.path.join(tmp, "target-project")
            shutil.copytree(fixture_src, target_repo)

            config = {
                "codex": {
                    "command": [
                        "node",
                        os.path.join(REPO_ROOT, "tests", "mocks", "codex_executor_mock_needs_reviewer.js"),
                    ],
                    "model": "gpt-5.2-codex",
                    "reasoning_effort": "xhigh",
                    "sandbox": "read-only",
                    "approval_policy": "never",
                },
                "claude_code": {
                    "command": ["node", os.path.join(REPO_ROOT, "tests", "mocks", "claude_reviewer_mock.js")],
                    "model": "opus",
                    "allowed_tools": ["Bash", "Read", "Edit", "Write", "Glob", "Grep"],
                    "max_turns": 1,
                },
                "agents": {
                    "reviewers": [{"id": "reviewer-1", "kind": "claude"}],
                    "executors": [{"id": "executor-1", "kind": "codex"}],
                    "assignment": {"executors_per_plan": 1},
                },
                "orchestrator": {
                    "multi_agent": True,
                    "max_iterations": 1,
                    "max_claude_question_rounds": 3,
                    "working_dir": os.path.join(tmp, "workspaces"),
                    "logs_dir": os.path.join(tmp, "logs"),
                    "workspace_strategy": "copy",
                    "use_git_worktree": False,
                    "cleanup": "always",
                    "apply_changes_on_success": True,
                    "commit_on_approval": False,
                    "ui": {"enabled": False},
                },
                "testing": {"timeout_sec": 60, "install_if_missing": False},
            }

            result = self._run_luigi(
                task="Fix division by zero in divide()",
                target_repo=target_repo,
                config=config,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=f"Luigi failed.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}\n",
            )

            divide_path = os.path.join(target_repo, "src", "divide.js")
            with open(divide_path, "r") as f:
                content = f.read()
            self.assertIn('throw new Error("Division by zero")', content)


if __name__ == "__main__":
    unittest.main()

