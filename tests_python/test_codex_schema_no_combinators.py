import json
import os
import unittest


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCHEMA_FILES = [
    "schemas/codex_plan.schema.json",
    "schemas/codex_review.schema.json",
    "schemas/reviewer_answer.schema.json",
    "schemas/reviewer_decision.schema.json",
    "schemas/executor_result.schema.json",
]

DISALLOWED_KEYS = {"oneOf", "allOf", "anyOf", "if", "then", "else"}


def _collect_disallowed(value: object, *, path: str, errors: list[str]) -> None:
    if isinstance(value, dict):
        for key in value.keys():
            if key in DISALLOWED_KEYS:
                errors.append(f"{path}: contains disallowed key {key!r}")
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            _collect_disallowed(child, path=child_path, errors=errors)
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            _collect_disallowed(child, path=f"{path}[{idx}]", errors=errors)


class CodexSchemaNoCombinatorsTest(unittest.TestCase):
    def test_schemas_do_not_use_disallowed_combinators(self) -> None:
        errors: list[str] = []
        for rel_path in SCHEMA_FILES:
            schema_path = os.path.join(REPO_ROOT, rel_path)
            with open(schema_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            _collect_disallowed(data, path=rel_path, errors=errors)
        if errors:
            self.fail("\n".join(errors))


if __name__ == "__main__":
    unittest.main()

