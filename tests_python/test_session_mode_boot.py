import json
import os
import tempfile
import unittest
from unittest import mock

import main


class SessionModeBootTest(unittest.TestCase):
    def test_session_mode_boots_without_name_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="luigi-session-") as tmp:
            repo_path = os.path.join(tmp, "repo")
            os.makedirs(repo_path, exist_ok=True)
            config = {
                "codex": {
                    "model": "gpt-5.2-codex",
                    "reasoning_effort": "xhigh",
                    "sandbox": "read-only",
                    "approval_policy": "never",
                },
                "claude_code": {
                    "model": "opus",
                    "allowed_tools": ["Read"],
                    "max_turns": 1,
                },
                "orchestrator": {
                    "max_iterations": 1,
                    "working_dir": os.path.join(tmp, "workspaces"),
                    "logs_dir": os.path.join(tmp, "logs"),
                    "workspace_strategy": "in_place",
                    "use_git_worktree": False,
                    "cleanup": "always",
                    "session_mode": True,
                    "ui": {"enabled": False},
                },
            }
            config_path = os.path.join(tmp, "config.json")
            with open(config_path, "w") as f:
                json.dump(config, f)

            argv = ["main.py", "test task", "--repo", repo_path, "--config", config_path]
            with mock.patch.object(
                main,
                "run_multi_agent_session",
                return_value={"approved": False, "persisted": False, "cleanup_workspace": None},
            ):
                with mock.patch.object(main, "start_streamlit_ui", return_value=None):
                    with mock.patch("sys.argv", argv):
                        main.main()


if __name__ == "__main__":
    unittest.main()
