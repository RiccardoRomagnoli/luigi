import os
import sys
import unittest

from codex_client import CodexClient


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class CodexClientApprovalPolicyTest(unittest.TestCase):
    def test_approval_policy_is_set_without_ask_flag(self) -> None:
        mock_path = os.path.join(
            REPO_ROOT, "tests", "mocks", "codex_mock_no_ask_for_approval.py"
        )
        config = {
            "command": [sys.executable, mock_path],
            "model": "gpt-5.3-codex",
            "reasoning_effort": "xhigh",
            "sandbox": "read-only",
            "approval_policy": "never",
        }
        client = CodexClient(config)

        result = client.create_plan("Test plan generation", cwd=REPO_ROOT)

        self.assertIn("claude_prompt", result)


if __name__ == "__main__":
    unittest.main()
