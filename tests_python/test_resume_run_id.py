import json
import os
import tempfile
import unittest

import main


class ResumeRunIdTest(unittest.TestCase):
    def test_load_resume_state_by_id(self) -> None:
        with tempfile.TemporaryDirectory(prefix="luigi-logs-") as tmp:
            run_id = "run-123"
            run_dir = os.path.join(tmp, run_id)
            os.makedirs(run_dir, exist_ok=True)

            repo_path = os.path.join(tmp, "repo")
            os.makedirs(repo_path, exist_ok=True)

            state = {"repo_path": repo_path, "run_status": "running"}
            with open(os.path.join(run_dir, "state.json"), "w") as f:
                json.dump(state, f)

            found = main._load_resume_state_by_id(
                logs_root=tmp,
                repo_path=repo_path,
                run_id=run_id,
            )
            self.assertIsNotNone(found)
            resume_id, resume_state = found
            self.assertEqual(resume_id, run_id)
            self.assertEqual(resume_state.get("repo_path"), repo_path)

    def test_load_resume_state_by_id_rejects_mismatch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="luigi-logs-") as tmp:
            run_id = "run-456"
            run_dir = os.path.join(tmp, run_id)
            os.makedirs(run_dir, exist_ok=True)

            state = {"repo_path": "/tmp/other", "run_status": "running"}
            with open(os.path.join(run_dir, "state.json"), "w") as f:
                json.dump(state, f)

            with self.assertRaises(RuntimeError):
                main._load_resume_state_by_id(
                    logs_root=tmp,
                    repo_path="/tmp/repo",
                    run_id=run_id,
                )


if __name__ == "__main__":
    unittest.main()
