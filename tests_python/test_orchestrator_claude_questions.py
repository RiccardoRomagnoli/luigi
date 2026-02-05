import json
import os
import shutil
import subprocess
import tempfile
import unittest


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class OrchestratorClaudeQuestionsTest(unittest.TestCase):
    def test_claude_questions_are_routed_to_codex(self) -> None:
        fixture_src = os.path.join(REPO_ROOT, "tests", "fixtures", "target-project")
        self.assertTrue(os.path.isdir(fixture_src))

        with tempfile.TemporaryDirectory(prefix="luigi-it-claude-q-") as tmp:
            target_repo = os.path.join(tmp, "target-project")
            shutil.copytree(fixture_src, target_repo)

            config_path = os.path.join(tmp, "config.mock.json")
            config = {
                "codex": {
                    "command": ["node", os.path.join(REPO_ROOT, "tests", "mocks", "codex_mock.js")],
                    "model": "gpt-5.3-codex",
                    "reasoning_effort": "xhigh",
                    "sandbox": "read-only",
                    "approval_policy": "never",
                },
                "claude_code": {
                    "command": ["node", os.path.join(REPO_ROOT, "tests", "mocks", "claude_mock_needs_codex.js")],
                    "model": "opus",
                    "allowed_tools": ["Bash", "Read", "Edit", "Write", "Glob", "Grep"],
                    "max_turns": 1,
                },
                "orchestrator": {
                    "max_iterations": 2,
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
            with open(config_path, "w") as f:
                json.dump(config, f)

            cmd = [
                "node",
                os.path.join(REPO_ROOT, "bin", "luigi.js"),
                "Fix division by zero in divide()",
                "--repo",
                target_repo,
                "--config",
                config_path,
            ]
            env = os.environ.copy()
            env["LUIGI_PYTHON"] = env.get("LUIGI_PYTHON", "python3")
            result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, env=env)
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

