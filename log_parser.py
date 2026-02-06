import json
import re
import shlex
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Literal, Optional


Source = Literal["codex", "claude"]


@dataclass(frozen=True)
class ActivityEvent:
    timestamp: Optional[datetime]
    source: Source
    text: str
    details: Optional[str] = None
    # Monotonic tiebreaker to keep stable ordering within identical timestamps.
    seq: int = 0


_SEGMENT_HEADER_RE = re.compile(r"^=== (?P<ts>[^ ]+) (?P<source>Codex|Claude) (?P<phase>.+) ===$")
_SEGMENT_EXIT_RE = re.compile(r"^=== (?P<source>Codex|Claude) exit (?P<code>-?\d+) ===$")


def _parse_iso(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _strip_markdown(s: str) -> str:
    s = s.strip()
    # remove common bullets/headers
    s = re.sub(r"^\s*[-*]\s+", "", s)
    s = re.sub(r"^\s*\d+\.\s+", "", s)
    s = re.sub(r"^\s*#+\s+", "", s)
    # remove bold markers
    s = s.replace("**", "")
    return s.strip()


def split_segments(lines: Iterable[str]) -> list[dict]:
    """Split a combined log into segments written by our wrappers.

    Each segment starts with: === <timestamp> <Codex|Claude> <phase> ===
    and ends when the next segment begins.
    """
    segments: list[dict] = []
    current: dict | None = None
    for raw in lines:
        line = raw.rstrip("\n")
        m = _SEGMENT_HEADER_RE.match(line)
        if m:
            if current:
                segments.append(current)
            current = {
                "timestamp": _parse_iso(m.group("ts")),
                "source": m.group("source").lower(),
                "phase": m.group("phase").strip(),
                "lines": [],
            }
            continue
        if current is None:
            continue
        current["lines"].append(line)
    if current:
        segments.append(current)
    return segments


_CMD_RESULT_RE = re.compile(
    r"^(?P<shell>/bin/\S+)\s+-lc\s+(?P<cmd>.+?)\s+in\s+(?P<cwd>\S+)\s+(?P<status>succeeded|failed).*?:$"
)


def _format_shell_command(cmd: str) -> tuple[str, Optional[str]]:
    cmd = cmd.strip()
    if (cmd.startswith("'") and cmd.endswith("'")) or (cmd.startswith('"') and cmd.endswith('"')):
        cmd = cmd[1:-1]
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = cmd.split()

    if not parts:
        return ("Running command", cmd or None)

    head = parts[0]
    args = parts[1:]

    def _first_non_flag() -> Optional[str]:
        for a in args:
            if not a.startswith("-"):
                return a
        return None

    if head == "ls":
        target = _first_non_flag() or "."
        return (f"Listing directory `{target}`", cmd)
    if head in ("cat", "less", "head", "tail"):
        target = _first_non_flag()
        if target:
            return (f"Reading file `{target}`", cmd)
        return ("Reading file(s)", cmd)
    if head in ("rg", "grep"):
        pattern = _first_non_flag()
        if pattern:
            return (f"Searching for `{pattern}`", cmd)
        return ("Searching", cmd)
    if head == "git":
        return (f"Running `git {' '.join(args)}`".strip(), cmd)
    if head == "npm":
        return (f"Running `npm {' '.join(args)}`".strip(), cmd)
    if head in ("python", "python3"):
        return (f"Running `{head} {' '.join(args)}`".strip(), cmd)
    if head == "node":
        return (f"Running `node {' '.join(args)}`".strip(), cmd)

    return (f"Running `{head}`", cmd)


def extract_codex_events(lines: list[str]) -> list[ActivityEvent]:
    segments = split_segments(lines)
    events: list[ActivityEvent] = []
    seq = 0
    for seg in segments:
        if seg.get("source") != "codex":
            continue
        ts = seg.get("timestamp")
        phase_raw = str(seg.get("phase") or "").strip().upper()
        phase_map = {
            "PLAN": "Planning",
            "REFINE_PLAN": "Refining plan",
            "REVIEW": "Reviewing",
            "ANSWER_EXECUTOR": "Answering executor",
        }
        phase = phase_map.get(phase_raw, phase_raw.lower() or "running")
        # Phase marker
        seq += 1
        events.append(
            ActivityEvent(timestamp=ts, source="codex", text=phase, details=None, seq=seq)
        )

        seg_lines: list[str] = seg.get("lines") or []
        in_exec = False
        pending_thinking_title = False
        for line in seg_lines:
            if not line.strip():
                continue

            if line.strip() == "thinking":
                pending_thinking_title = True
                continue

            if pending_thinking_title:
                title = _strip_markdown(line)
                if title:
                    seq += 1
                    events.append(
                        ActivityEvent(
                            timestamp=ts,
                            source="codex",
                            text=title,
                            details=None,
                            seq=seq,
                        )
                    )
                    pending_thinking_title = False
                continue

            if line.strip() == "exec":
                in_exec = True
                continue

            if in_exec:
                m = _CMD_RESULT_RE.match(line.strip())
                if m:
                    human, details = _format_shell_command(m.group("cmd"))
                    status = m.group("status")
                    cwd = m.group("cwd")
                    suffix = "" if status == "succeeded" else " (failed)"
                    seq += 1
                    events.append(
                        ActivityEvent(
                            timestamp=ts,
                            source="codex",
                            text=f"{human}{suffix}",
                            details=f"cwd: {cwd}\ncmd: {details}" if details else f"cwd: {cwd}",
                            seq=seq,
                        )
                    )
                    in_exec = False
                    continue
                # If we didn't get a recognizable command line, keep scanning a bit.

            # Detect plan JSON output and summarize it
            if line.lstrip().startswith("{") and '"claude_prompt"' in line and '"tasks"' in line:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    tasks = obj.get("tasks") or []
                    test_cmds = obj.get("test_commands")
                    tc_len = len(test_cmds) if isinstance(test_cmds, list) else 0
                    seq += 1
                    events.append(
                        ActivityEvent(
                            timestamp=ts,
                            source="codex",
                            text=f"Produced plan ({len(tasks)} tasks, {tc_len} test commands)",
                            details=None,
                            seq=seq,
                        )
                    )
    return events


def extract_claude_events(lines: list[str]) -> list[ActivityEvent]:
    segments = split_segments(lines)
    events: list[ActivityEvent] = []
    seq = 0
    for seg in segments:
        if seg.get("source") != "claude":
            continue
        ts = seg.get("timestamp")
        phase_raw = str(seg.get("phase") or "").strip().lower()
        phase = "Implementing" if phase_raw.startswith("implement") else (phase_raw or "running")
        seq += 1
        events.append(ActivityEvent(timestamp=ts, source="claude", text=phase, seq=seq))

        seg_lines: list[str] = seg.get("lines") or []
        # Find the JSON blob (Claude stdout)
        json_line = None
        for line in seg_lines:
            if line.lstrip().startswith("{") and line.rstrip().endswith("}"):
                json_line = line.strip()
                break
        if not json_line:
            continue
        try:
            payload = json.loads(json_line)
        except Exception:
            continue

        structured = payload.get("structured_output") if isinstance(payload, dict) else None
        if isinstance(structured, dict):
            status = structured.get("status")
            if status:
                seq += 1
                events.append(
                    ActivityEvent(timestamp=ts, source="claude", text=str(status), seq=seq)
                )
            summary = structured.get("summary")
            if isinstance(summary, str) and summary.strip():
                for raw_line in summary.splitlines():
                    cleaned = _strip_markdown(raw_line)
                    if not cleaned:
                        continue
                    seq += 1
                    events.append(
                        ActivityEvent(timestamp=ts, source="claude", text=cleaned, seq=seq)
                    )
    return events


def merge_events(*batches: list[ActivityEvent]) -> list[ActivityEvent]:
    all_events: list[ActivityEvent] = []
    for batch in batches:
        all_events.extend(batch)
    all_events.sort(
        key=lambda e: (
            e.timestamp or datetime.min,
            0 if e.source == "codex" else 1,
            e.seq,
        )
    )
    return all_events

