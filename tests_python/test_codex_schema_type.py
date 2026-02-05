import json
import os
import unittest


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCHEMA_FILES = [
    "schemas/codex_plan.schema.json",
    "schemas/codex_review.schema.json",
    "schemas/codex_answer.schema.json",
    "schemas/reviewer_decision.schema.json",
    "schemas/executor_result.schema.json",
]


class CodexSchemaTypeTest(unittest.TestCase):
    def test_top_level_schema_type_is_object(self) -> None:
        for rel_path in SCHEMA_FILES:
            schema_path = os.path.join(REPO_ROOT, rel_path)
            with open(schema_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertEqual(
                data.get("type"),
                "object",
                msg=f"{rel_path} must declare top-level type 'object'",
            )


if __name__ == "__main__":
    unittest.main()
