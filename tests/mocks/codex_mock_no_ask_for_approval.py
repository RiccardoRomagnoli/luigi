import json
import sys


def _get_arg_value(argv: list[str], flag: str) -> str | None:
    if flag not in argv:
        return None
    idx = argv.index(flag)
    if idx + 1 >= len(argv):
        return None
    return argv[idx + 1]


def _has_approval_policy_override(argv: list[str]) -> bool:
    for idx, arg in enumerate(argv):
        if arg in ("-c", "--config"):
            if idx + 1 < len(argv) and str(argv[idx + 1]).startswith("approval_policy="):
                return True
    return False


def main() -> None:
    argv = sys.argv[1:]
    if "--ask-for-approval" in argv:
        sys.stderr.write("error: unexpected argument '--ask-for-approval' found\n")
        sys.exit(2)

    output_last_message = _get_arg_value(argv, "--output-last-message")
    if not output_last_message:
        sys.stderr.write("Missing --output-last-message\n")
        sys.exit(2)

    if not _has_approval_policy_override(argv):
        sys.stderr.write("Missing approval_policy config override\n")
        sys.exit(2)

    response = {
        "status": "OK",
        "claude_prompt": "Test prompt",
        "tasks": [
            {
                "id": "1",
                "title": "Mock task",
                "description": "Mock task description",
                "acceptance_criteria": [],
                "suggested_commands": [],
            }
        ],
        "test_commands": [
            {
                "id": "unit",
                "kind": "unit",
                "label": None,
                "command": ["echo", "ok"],
                "timeout_sec": None,
            }
        ],
        "questions": [],
        "notes": "Mock plan for approval policy test.",
    }
    with open(output_last_message, "w", encoding="utf-8") as handle:
        json.dump(response, handle)

    sys.exit(0)


if __name__ == "__main__":
    main()
