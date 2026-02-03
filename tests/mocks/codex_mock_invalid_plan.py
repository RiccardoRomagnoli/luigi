import json
import sys


def _get_arg_value(argv: list[str], flag: str) -> str | None:
    if flag not in argv:
        return None
    idx = argv.index(flag)
    if idx + 1 >= len(argv):
        return None
    return argv[idx + 1]


def main() -> None:
    argv = sys.argv[1:]
    output_last_message = _get_arg_value(argv, "--output-last-message")
    if not output_last_message:
        sys.stderr.write("Missing --output-last-message\n")
        sys.exit(2)

    # Intentionally invalid: status OK but missing required plan content.
    response = {
        "status": "OK",
        "claude_prompt": None,
        "tasks": [],
        "test_commands": [],
        "questions": [],
        "notes": None,
    }
    with open(output_last_message, "w", encoding="utf-8") as handle:
        json.dump(response, handle)

    sys.exit(0)


if __name__ == "__main__":
    main()
