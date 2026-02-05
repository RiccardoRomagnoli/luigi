import json
import os
import tempfile
import unittest
from unittest import mock

import main


class ResumeOrchestratorModeTest(unittest.TestCase):
    def test_resume_honors_persisted_multi_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="luigi-resume-") as tmp:
            repo_path = os.path.join(tmp, "repo")
            os.makedirs(repo_path, exist_ok=True)

            logs_root = os.path.join(tmp, "logs")
            run_id = "run-123"
            run_dir = os.path.join(logs_root, run_id)
            os.makedirs(run_dir, exist_ok=True)
            with open(os.path.join(run_dir, "state.json"), "w") as f:
                json.dump(
                    {
                        "run_id": run_id,
                        "repo_path": repo_path,
                        "run_status": "running",
                        "run_completed": False,
                        "task": "do something",
                        "workspace_strategy": "in_place",
                        "workspace_path": repo_path,
                        "orchestrator_mode": "multi",
                    },
                    f,
                )

            config_path = os.path.join(tmp, "config.json")
            with open(config_path, "w") as f:
                json.dump(
                    {
                        "codex": {
                            "model": "gpt-5.3-codex",
                            "reasoning_effort": "xhigh",
                            "sandbox": "read-only",
                            "approval_policy": "never",
                        },
                        "claude_code": {
                            "model": "opus",
                            "allowed_tools": ["Read"],
                            "max_turns": 1,
                        },
                        "agents": {
                            "reviewers": [{"id": "reviewer-1", "kind": "codex"}],
                            "executors": [{"id": "executor-1", "kind": "claude"}],
                        },
                        "orchestrator": {
                            "logs_dir": logs_root,
                            "working_dir": os.path.join(tmp, "workspaces"),
                            "session_mode": False,
                            "ui": {"enabled": False},
                        },
                    },
                    f,
                )

            argv = ["main.py", repo_path, "--resume-run-id", run_id, "--config", config_path]
            with mock.patch.object(main, "start_streamlit_ui", return_value=None):
                with mock.patch.object(
                    main,
                    "run_multi_agent_session",
                    return_value={"approved": False, "persisted": False, "cleanup_workspace": None},
                ) as mocked_multi:
                    with mock.patch("sys.argv", argv):
                        main.main()

            self.assertTrue(mocked_multi.called, "Expected multi-agent session on resume.")


if __name__ == "__main__":
    unittest.main()

