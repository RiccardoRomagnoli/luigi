from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class AgentSpec:
    id: str
    kind: str  # "codex" | "claude"
    role: str  # "reviewer" | "executor"
    command: Optional[List[str]] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    sandbox: Optional[str] = None
    approval_policy: Optional[str] = None
    allowed_tools: Optional[List[str]] = None
    max_turns: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)


def _normalize_agent_spec(raw: Dict[str, Any], *, role: str, index: int) -> AgentSpec:
    agent_id = str(raw.get("id") or f"{role}-{index+1}")
    kind = str(raw.get("kind") or "codex").lower()
    command = raw.get("command")
    if isinstance(command, str):
        command = [command]
    if command is not None and not isinstance(command, list):
        command = None
    allowed_tools = raw.get("allowed_tools")
    if allowed_tools is not None and not isinstance(allowed_tools, list):
        allowed_tools = None
    max_turns = raw.get("max_turns")
    max_turns = int(max_turns) if isinstance(max_turns, (int, float, str)) and str(max_turns).isdigit() else None
    return AgentSpec(
        id=agent_id,
        kind=kind,
        role=role,
        command=command,
        model=raw.get("model"),
        reasoning_effort=raw.get("reasoning_effort"),
        sandbox=raw.get("sandbox"),
        approval_policy=raw.get("approval_policy"),
        allowed_tools=allowed_tools,
        max_turns=max_turns,
        extra={k: v for k, v in raw.items() if k not in {"id", "kind", "command", "model", "reasoning_effort", "sandbox", "approval_policy", "allowed_tools", "max_turns"}},
    )


def normalize_agents(config: Dict[str, Any]) -> Dict[str, List[AgentSpec]]:
    agents_cfg = config.get("agents", {}) if isinstance(config, dict) else {}
    reviewers_raw = agents_cfg.get("reviewers")
    executors_raw = agents_cfg.get("executors")

    reviewers = []
    if isinstance(reviewers_raw, list) and reviewers_raw:
        reviewers = [_normalize_agent_spec(raw, role="reviewer", index=i) for i, raw in enumerate(reviewers_raw)]
    else:
        reviewers = [_normalize_agent_spec({"id": "reviewer-1", "kind": "codex"}, role="reviewer", index=0)]

    executors = []
    if isinstance(executors_raw, list) and executors_raw:
        executors = [_normalize_agent_spec(raw, role="executor", index=i) for i, raw in enumerate(executors_raw)]
    else:
        executors = [_normalize_agent_spec({"id": "executor-1", "kind": "claude"}, role="executor", index=0)]

    return {"reviewers": reviewers, "executors": executors}


def assignment_config(config: Dict[str, Any]) -> Dict[str, Any]:
    agents_cfg = config.get("agents", {}) if isinstance(config, dict) else {}
    assignment = agents_cfg.get("assignment", {}) if isinstance(agents_cfg, dict) else {}
    mode = str(assignment.get("mode") or "round_robin")
    executors_per_plan = assignment.get("executors_per_plan")
    try:
        executors_per_plan = int(executors_per_plan)
    except Exception:
        executors_per_plan = 1
    if executors_per_plan < 1:
        executors_per_plan = 1
    return {"mode": mode, "executors_per_plan": executors_per_plan}

