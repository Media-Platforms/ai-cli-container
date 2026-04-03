# Agent Guidance

## Core Heuristic

Do not start from “what code can I edit quickly.” Start from “which layer owns
this behavior.”

Most mistakes in this repo come from editing the wrong layer:

- host concern edited in `start-ai-cli` instead of `claude-container`
- image-content concern edited in the launcher instead of `Dockerfile`
- Docker policy concern edited in docs without changing the proxy
- Claude-plugin concern edited in generic runtime code

## Component Selection Heuristic

Ask these questions in order:

1. Does the behavior happen before `docker run` starts?
   If yes, start with `claude-container`.
2. Does it change what exists inside the image?
   If yes, start with `Dockerfile`.
3. Does it change what happens at container boot, before the main tool runs?
   If yes, start with `start-ai-cli`.
4. Does it change what nested Docker commands are allowed to do?
   If yes, start with `docker-socket-proxy/docker_socket_proxy.py`.
5. Does it change Claude-specific subagents or the Pdb MCP server?
   If yes, start with `container-plugin/`.

## Change Discipline

- Preserve the repo’s preference for small, direct Bash and Python code.
- Keep shell quoting conservative.
- Keep security-sensitive logic explicit and test-backed.
- Update `README.md` for user-visible behavior changes.
- Update memory-bank docs when the repo’s decision rules or architecture change.

## Security-Sensitive Areas

Treat these areas as high scrutiny:

- new mounts from host into container
- new environment-variable pass-through
- looser Docker proxy policy
- any shift toward more root execution
- auth fallback logic that reads credentials from host systems

For these, explain the reason for the change and validate as far as the
environment allows.

## Known Non-Obvious Behaviors

- `codex-container` and `gemini-container` are not separate scripts; behavior is
  selected by basename.
- `codex-container` injects `codex --yolo` only when the command looks like a
  normal Codex CLI launch, not when running an arbitrary command such as `bash`.
- `start-ai-cli` only wraps Claude when the first arg is empty or starts with
  `-`. A direct command bypasses Claude-specific defaults and plugin injection.
- Docker proxy write access is limited to the current working directory; sibling
  directories under the same parent may only be mounted read-only.
- The Pdb MCP server currently supports one active session at a time.

## What Good Changes Look Like

A good change in this repo usually has these properties:

- the smallest responsible edit surface
- tests updated when policy or protocol behavior changes
- README updated when user behavior changes
- no unnecessary broadening of trust boundaries
- no duplication of configuration across layers unless required

## What Weak Changes Look Like

Common failure modes:

- adding environment handling in multiple places without a clear owner
- changing Docker policy without adding rejection-path tests
- documenting behavior that the code does not actually implement
- patching around a symptom in `start-ai-cli` when the real issue is in the host
  launcher
- forgetting that the repo supports three tool flavors, not just Claude
