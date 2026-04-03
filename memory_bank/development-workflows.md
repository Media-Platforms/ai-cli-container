# Development Workflows

This document describes the expected thought process for common task types.

## Workflow: Add Or Change Launcher Behavior

Examples:

- support a new env var
- mount another host config directory
- tweak argument injection
- adjust Codex or Gemini startup behavior

Suggested sequence:

1. Read `ai-cli-container` and confirm whether the behavior is host-side.
2. Check whether `start-ai-cli` also needs to change for in-container handling.
3. Update `README.md` if user-visible behavior changes.
4. Update memory-bank docs if the change affects future agent decision-making.
5. Validate shell syntax and, if possible, exercise the launcher path safely.

Things to watch:

- Preserve strict shell mode.
- Prefer arrays for Docker args and mounts.
- Be careful with optional environment expansion and quoting.
- Do not assume all optional host config files exist.

## Workflow: Change Container Image Contents

Examples:

- install another CLI
- add system packages
- change Python or Node dependencies
- copy additional plugin assets

Suggested sequence:

1. Edit `Dockerfile`.
2. Decide whether the installed tool needs host config mounting in
   `ai-cli-container`.
3. Decide whether the entrypoint needs to expose or wrap it.
4. Update docs.
5. Build the image or, at minimum, sanity-check the Dockerfile logic.

Things to watch:

- Keep layers intentional; build-time-only dependencies should not leak into the
  final image unnecessarily.
- Match install user context to where the tool expects to live.
- If a path is referenced later by `start-ai-cli`, `README.md`, or tests, keep
  those in sync.

## Workflow: Tighten Or Relax Docker Safety Policy

Examples:

- block a new dangerous flag
- permit a currently blocked scenario
- extend validation to another Docker API request

Suggested sequence:

1. Read `docker_socket_proxy.py` and its existing tests before editing.
2. Make the smallest policy change that satisfies the task.
3. Add or update tests for both acceptance and rejection paths.
4. Re-read the threat boundary implications in `architecture.md`.
5. Update user-facing docs if behavior materially changes.

Things to watch:

- Favor explicit policy checks over vague heuristics.
- Preserve Unix-socket HTTP proxy correctness, not just policy logic.
- Avoid accidental breakage of keep-alive, chunked transfer, or upgraded
  connections.

## Workflow: Improve Claude Plugin Or MCP Behavior

Examples:

- add a new Claude subagent prompt
- change the memory-bank workflow
- add another MCP tool
- improve Pdb session lifecycle

Suggested sequence:

1. Confirm the change is Claude-plugin-specific and not a generic launcher or
   image concern.
2. Edit files under `container-plugin/`.
3. Update tests when touching Python MCP code.
4. If the plugin depends on image paths or installed packages, confirm
   `Dockerfile` still matches reality.

Things to watch:

- `pdb_mcp_server.py` maintains global singleton session state.
- The MCP server communicates over stdio; avoid changes that assume a network
  transport.
- The `python-test-debugger` prompt should stay aligned with actual tool names
  and supported parameters.

## Workflow: Add Memory For Future Agents

This current task is an example. Good memory-bank additions should:

- capture stable repo truths and durable decision rules
- explain why a component exists, not just list files
- call out security-sensitive surfaces and coupling
- avoid copying large amounts of implementation text verbatim
- distinguish public user contract from internal implementation details

Do not use the memory bank as a dumping ground for ephemeral work logs.

## Workflow: Update Public Docs

If a task changes behavior that an end user would notice, update `README.md`.
The memory bank is not a substitute for user documentation.

Rule of thumb:

- README: what users need to know to install, launch, authenticate, and
  troubleshoot
- memory bank: what coding agents need to know to edit the repo correctly

## When To Be Cautious

Slow down and inspect more deeply when a task touches:

- secret-bearing mounts
- Docker daemon access
- root vs `dev` execution boundaries
- auth fallback logic
- CLI-selection behavior by executable name
- test harnesses that simulate low-level HTTP or async process behavior
