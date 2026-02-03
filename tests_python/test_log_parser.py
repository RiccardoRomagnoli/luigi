import unittest

from log_parser import extract_claude_events, extract_codex_events, merge_events


class LogParserTest(unittest.TestCase):
    def test_extract_codex_events_from_exec_and_thinking(self) -> None:
        lines = [
            "=== 2026-02-01T16:10:45.000000 Codex PLAN ===",
            "user",
            "thinking",
            "**Checking project directory structure**",
            "exec",
            "/bin/zsh -lc 'ls apps' in /Users/ricrom/Code/overwrite succeeded in 250ms:",
            "backend",
            "extension",
            "codex",
            '{"claude_prompt":"x","tasks":[{"id":"1","title":"t","description":"d"}],"test_commands":[],"status":"OK","questions":[],"notes":null}',
            "=== Codex exit 0 ===",
        ]
        events = extract_codex_events(lines)
        texts = [e.text for e in events]
        self.assertTrue(any("Checking project directory structure" in t for t in texts))
        self.assertTrue(any("Listing directory" in t and "`apps`" in t for t in texts))
        self.assertTrue(any("Produced plan" in t for t in texts))

    def test_extract_claude_events_from_structured_summary(self) -> None:
        lines = [
            "=== 2026-02-01T16:14:27.000000 Claude implement ===",
            "--- Claude stdout ---",
            '{"structured_output":{"status":"DONE","summary":"Fixed X\\n\\n- Added Y\\n"}}',
            "=== Claude exit 0 ===",
        ]
        events = extract_claude_events(lines)
        texts = [e.text for e in events]
        self.assertTrue(any(t == "DONE" for t in texts))
        self.assertTrue(any(t == "Fixed X" for t in texts))
        self.assertTrue(any(t == "Added Y" for t in texts))

    def test_merge_events_preserves_sorting(self) -> None:
        codex = extract_codex_events(
            [
                "=== 2026-02-01T10:00:00 Codex PLAN ===",
                "thinking",
                "**A**",
            ]
        )
        claude = extract_claude_events(
            [
                "=== 2026-02-01T11:00:00 Claude implement ===",
                "--- Claude stdout ---",
                '{"structured_output":{"status":"DONE","summary":"B"}}',
            ]
        )
        merged = merge_events(codex, claude)
        self.assertGreater(len(merged), 0)
        # codex event should come before claude event because of timestamp ordering
        self.assertEqual(merged[0].source, "codex")


if __name__ == "__main__":
    unittest.main()

