# Components And Ownership

This document maps change requests to likely edit surfaces.

## Root Files

### `README.md`

Owns end-user documentation. Update it when changing:

- install steps
- supported CLIs
- mounted paths
- environment variables
- authentication expectations
- Docker proxy behavior
- troubleshooting guidance

Do not let the memory bank become the only place where user-visible behavior is
documented. The README is still the public contract.

### `Makefile`

Owns common local workflows:

- `build`
- `update`
- `test`
- `install`
- `clean`

If a task adds another routine developer workflow, decide whether it belongs in
`Makefile` or should remain an ad hoc command. Prefer adding a target when the
workflow is repeatable and repo-specific.

### `Dockerfile`

Edit this when changing:

- base image
- OS packages
- npm or pip installed tools
- which repo assets are copied into the image
- default entrypoint
- ownership or user setup

Avoid making host-launch decisions here. `Dockerfile` should describe the image,
not host environment discovery.

### `claude-container`

Edit this when changing:

- launcher CLI UX
- which environment variables are passed through
- which host paths are mounted
- how different tool flavors are selected
- preflight checks before `docker run`
- auth fallback behavior on the host

This script is a host-side compatibility surface. Be careful with shell
portability and quoting. It already uses strict mode and array-based volume
assembly; preserve that style.

### `start-ai-cli`

Edit this when changing:

- proxy startup
- privilege drop behavior
- Claude default arguments
- arbitrary-command vs Claude-command dispatch
- default model behavior

Do not move host-specific logic here. This script runs inside the container.

## `docker-socket-proxy/`

### `docker_socket_proxy.py`

This is the main policy engine. Typical reasons to edit it:

- add or relax a blocked Docker capability
- change which API requests are validated
- tighten path handling
- improve HTTP proxy correctness
- add diagnostics or logging

This file is security-sensitive. Any policy change should be paired with tests
that prove the new allowed and rejected cases.

### `test_docker_socket_proxy.py`

This test suite is extensive relative to repo size. It covers:

- path validation helpers
- bind and mount policy decisions
- JSON validation
- HTTP header parsing
- chunked transfer forwarding
- stream forwarding and splice behavior
- end-to-end proxy request handling

If you modify the proxy without touching tests, that is usually a smell.

## `container-plugin/`

### `agents/memory-bank-analyzer.md`

Claude subagent prompt telling another agent to read `memory_bank` after a task
is known. Update this only if the memory-bank workflow itself changes.

### `codex-agents/memory-bank-analyzer.toml`

Codex subagent equivalent of the Claude `agents/memory-bank-analyzer.md`.
Copied to `~/.codex/agents/` by `start-ai-cli` at container startup.
Keep in sync with the Claude subagent.

### `gemini-agents/memory-bank-analyzer.md`

Gemini subagent equivalent of the Claude `agents/memory-bank-analyzer.md`.
Copied to `~/.gemini/agents/` by `start-ai-cli` at container startup.
Keep in sync with the Claude subagent.

### `agents/python-test-debugger.md`

Claude subagent prompt for using the MCP Pdb server during difficult Python test
debugging. Update when the debugging workflow or available tools change.

### `pdb_mcp_server.py`

Owns the MCP interface for interactive Pdb sessions. Typical reasons to edit:

- add a new MCP tool or argument
- improve process lifecycle handling
- change how output is buffered or prompt detection works
- improve Docker Compose invocation behavior

The current implementation manages exactly one active session globally.

### `test_pdb_mcp_server.py`

Owns coverage for:

- session lifecycle
- prompt detection
- output buffering
- MCP tool handlers
- main server registration

As with the proxy, behavior changes here should normally come with tests.

## Decision Matrix

### “I need to change what gets mounted into the container.”

Primary file: `claude-container`

Also consider:

- `README.md`
- `memory_bank/architecture.md`

### “I need to install another tool in the image.”

Primary file: `Dockerfile`

Also consider:

- whether the tool belongs under `dev` or root ownership
- whether host config for that tool also needs mounting in `claude-container`
- whether `README.md` should expose it

### “I need to change the default Claude invocation.”

Primary file: `start-ai-cli`

Also consider whether the behavior should differ for arbitrary commands versus
interactive Claude startup.

### “I need to support a new launcher alias.”

Primary files:

- `Makefile`
- `claude-container`
- `README.md`

Potentially `Dockerfile` only if the new tool must be installed in the image.

### “I need to change Docker safety policy.”

Primary files:

- `docker-socket-proxy/docker_socket_proxy.py`
- `docker-socket-proxy/test_docker_socket_proxy.py`

Also consider `README.md` because policy changes affect user expectations.

### “I need to improve debugging support for Python tests in downstream repos.”

Primary files:

- `container-plugin/pdb_mcp_server.py`
- `container-plugin/test_pdb_mcp_server.py`
- `container-plugin/agents/python-test-debugger.md`

## Files That Are Deliberately Small But Important

- `start-ai-cli`: short, but central to runtime security posture
- `claude-container`: short, but defines most host/container integration behavior
- `Makefile`: short, but part of the supported UX

Agents should not mistake file length for low importance.
