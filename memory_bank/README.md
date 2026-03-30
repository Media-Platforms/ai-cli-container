# Memory Bank

This directory exists for coding agents that need repo-specific context before
making changes. The intended flow is:

1. Understand the user task.
2. Read the memory bank.
3. Decide which component owns the change.
4. Inspect only the code paths relevant to that task.

The docs here are intentionally thorough. They are meant to be summarized by
subagents, not skimmed manually during every task.

## Reading Order

- `repo-overview.md`: What this repository is for, what is shipped, and what is
  intentionally out of scope.
- `architecture.md`: Runtime model, startup flow, trust boundaries, and how the
  major pieces interact.
- `components.md`: File-by-file ownership and where specific categories of
  changes should land.
- `development-workflows.md`: Common task playbooks for adding features,
  changing launch behavior, or tightening Docker policy.
- `testing-and-validation.md`: What tests exist, how to run them, and what
  validation is expected for each kind of change.
- `agent-guidance.md`: Decision heuristics for coding agents working in this
  repo.

## Fast Orientation

This repo builds a Docker image that provides an isolated coding environment for
Claude Code, Codex, and Gemini. The root launcher script chooses which tool to
run based on the executable name (`claude-container`, `codex-container`,
`gemini-container`), mounts host configuration into the container, and exposes
Docker through a filtered Unix socket proxy.

The codebase is small but the behavior is security-sensitive. Most changes fall
into one of these buckets:

- Host-side launcher behavior: `claude-container`
- Container image contents or defaults: `Dockerfile`
- Container startup and privilege dropping: `start-claude`
- Docker API restrictions: `docker-socket-proxy/docker_socket_proxy.py`
- Claude plugin assets and MCP tooling: `container-plugin/`

If a task touches container behavior and Docker access, read
`architecture.md` before editing.
