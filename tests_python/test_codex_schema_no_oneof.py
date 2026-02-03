import json
import os
import unittest


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCHEMA_FILES = [
    "schemas/codex_plan.schema.json",
    "schemas/codex_review.schema.json",
    "schemas/codex_answer.schema.json",
]


def _contains_oneof(value: object) -> bool:
    if isinstance(value, dict):
        if "oneOf" in value:
            return True
        return any(_contains_oneof(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_oneof(child) for child in value)
    return False


class CodexSchemaNoOneOfTest(unittest.TestCase):
    def test_schemas_do_not_use_oneof(self) -> None:
        for rel_path in SCHEMA_FILES:
            schema_path = os.path.join(REPO_ROOT, rel_path)
            with open(schema_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertFalse(
                _contains_oneof(data),
                msg=f"{rel_path} must not contain 'oneOf'",
            )


if __name__ == "__main__":
    unittest.main()
