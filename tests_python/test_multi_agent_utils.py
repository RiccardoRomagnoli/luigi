import unittest

import main
from agents import AgentSpec


class MultiAgentUtilsTest(unittest.TestCase):
    def test_assign_executors_round_robin(self) -> None:
        reviewers = ["r1", "r2"]
        executors = [
            AgentSpec(id="e1", kind="claude", role="executor"),
            AgentSpec(id="e2", kind="codex", role="executor"),
        ]
        assignments = main._assign_executors(reviewers, executors, executors_per_plan=2)
        self.assertEqual(len(assignments), 4)
        self.assertEqual(assignments[0]["executor"].id, "e1")
        self.assertEqual(assignments[1]["executor"].id, "e2")
        self.assertEqual(assignments[2]["executor"].id, "e1")

    def test_compute_consensus(self) -> None:
        decisions = {
            "r1": {"status": "REJECTED", "winner_candidate_id": "c1", "next_prompt": "do X"},
            "r2": {"status": "REJECTED", "winner_candidate_id": "c1", "next_prompt": "do X"},
        }
        result = main._compute_consensus(decisions)
        self.assertTrue(result["consensus"])
        self.assertEqual(result["winner"], "c1")

    def test_validate_reviewer_decision_disallows_next_prompt_on_approved(self) -> None:
        with self.assertRaises(RuntimeError):
            main._validate_reviewer_decision(
                {
                    "status": "APPROVED",
                    "winner_candidate_id": "c1",
                    "summary": "ok",
                    "feedback": "minor",
                    "next_prompt": "do more work",
                    "questions": [],
                    "notes": None,
                },
                {"c1"},
            )

    def test_parse_admin_choice(self) -> None:
        parsed = main._parse_admin_choice("choose 2\nnotes: add context\nextra line")
        self.assertEqual(parsed["choice"], 2)
        self.assertIn("add context", parsed["notes"])


if __name__ == "__main__":
    unittest.main()

