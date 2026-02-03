import json
import os
import unittest


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCHEMA_FILES = [
    "schemas/codex_plan.schema.json",
    "schemas/codex_review.schema.json",
    "schemas/codex_answer.schema.json",
]


def _collect_required_errors(value: object, *, path: str, errors: list[str]) -> None:
    if isinstance(value, dict):
        if value.get("type") == "object" or "properties" in value:
            properties = value.get("properties", {})
            if properties:
                required = value.get("required")
                if not isinstance(required, list):
                    errors.append(f"{path}: missing required list for object properties")
                else:
                    missing = set(properties.keys()) - set(required)
                    if missing:
                        errors.append(f"{path}: required missing keys {sorted(missing)}")
                if value.get("additionalProperties") is not False:
                    errors.append(f"{path}: additionalProperties must be false")
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            _collect_required_errors(child, path=child_path, errors=errors)
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            _collect_required_errors(child, path=f"{path}[{idx}]", errors=errors)


class CodexSchemaRequiredFieldsTest(unittest.TestCase):
    def test_all_object_fields_are_required(self) -> None:
        for rel_path in SCHEMA_FILES:
            schema_path = os.path.join(REPO_ROOT, rel_path)
            with open(schema_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            errors: list[str] = []
            _collect_required_errors(data, path=rel_path, errors=errors)
            if errors:
                self.fail("\n".join(errors))


if __name__ == "__main__":
    unittest.main()
