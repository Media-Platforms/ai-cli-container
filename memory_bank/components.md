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

Do not give `dev` sudo rights here. That is a security decision, not an
ergonomic one: see "Boundary 3" in `architecture.md`. Runtime `apt install` and
`npm install -g` are not available as a result, so new packages go in this file.

### `ai-cli-container` (installed as `claude-container`)

Edit this when changing:

- launcher CLI UX
- which environment variables are passed through
- which host paths are mounted
- how different tool flavors are selected
- preflight checks before `docker run`
- auth fallback behavior on the host
- which backend Claude Code talks to

This script is a host-side compatibility surface. Be careful with shell
portability and quoting. It already uses strict mode and array-based volume
assembly; preserve that style.

Secrets are passed as `-e NAME` with no value, which makes `docker run` copy the
value from the launcher's own environment rather than take it as an argument any
local user could read with `ps`. A new secret therefore needs two edits: add it
to the `export` list before `docker run`, and pass it as `-e NAME`. Passing one
as `-e NAME=value` puts it back in the host process list, and passing `-e NAME`
without exporting it silently drops it, because `docker run` skips a variable
that is not in its environment.

Backend selection lives here, not in `start-ai-cli`. `AI_CLI_BACKEND` picks
`bedrock`, `gateway`, or `anthropic`, and is inferred from the environment when
unset. Each case owns the full set of Anthropic environment variables it passes
through, including clearing the ones that belong to the other backends, so a
host environment configured for one backend cannot leak into another.

Settings that must override the mounted `~/.claude/settings.json` travel to the
container in `AI_CLI_CLAUDE_SETTINGS`, which `start-ai-cli` passes to
`claude --settings`. That is how a host `apiKeyHelper` — normally a macOS
`security` call that cannot run in the container — gets replaced: with nothing
for Bedrock, and with `printenv AI_CLI_ANTHROPIC_API_KEY` for the gateway. Keep
that variable a small, launcher-authored JSON object.

The gateway key travels in `AI_CLI_ANTHROPIC_API_KEY` rather than in
`ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` on purpose: each of those sends
the key only one way, as an `x-api-key` header or as a bearer token, and setting
both makes Claude Code warn that the credentials conflict. An `apiKeyHelper`
sends both headers, which is what the host setup does and what LiteLLM-style
proxies expect.

### `start-ai-cli`

Edit this when changing:

- proxy startup
- privilege drop behavior
- Claude default arguments
- arbitrary-command vs Claude-command dispatch
- default model behavior
- how launcher-chosen Claude settings are applied (`AI_CLI_CLAUDE_SETTINGS`)

Do not move host-specific logic here. This script runs inside the container.

## `docker-socket-proxy/`

### `docker_socket_proxy.py`

This is the main policy engine. Typical reasons to edit it:

- allow a Docker API endpoint the proxy does not yet recognize, which is what a
  "docker: permission denied" or 403 from a normal workflow means
- add a capability to `ALLOWED_CAPS`, or classify a new `HostConfig` field into
  `PASS_FIELDS`, `CHECKED_FIELDS`, `NULL_ONLY_FIELDS` or `EMPTY_ONLY_FIELDS`
- change which API requests are validated
- tighten path handling
- improve HTTP proxy correctness
- add diagnostics or logging

This file is security-sensitive. Any policy change should be paired with tests
that prove the new allowed and rejected cases.

Two design rules matter more than the individual checks. First, the proxy is an
allowlist in both directions: an unmatched endpoint and an unclassified
`HostConfig` field are both rejected, so a new Docker feature fails closed.
Second, it must parse a request the way the daemon does — version prefix
stripped, path percent-decoded, JSON field names matched case-insensitively, a
repeated field rejected — because any disagreement between the two parsers is a
way past every check. `TestRealPayload` holds a verbatim `HostConfig` captured
from a real `docker run` and is what catches an allowlist that has become too
narrow for an ordinary command.

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

Claude subagent prompt telling another agent to read `memory_bank/` and/or
`memory-bank/` after a task is known. The caller owns checking which of the two
directories exist; the subagent assumes at least one exists and only summarizes
files in them. Update this only if the memory-bank
workflow itself changes.

### `codex-agents/memory-bank-analyzer.toml`

Codex subagent for memory-bank analysis. Tuned for fast completion
(`model_reasoning_effort = "low"`, `sandbox_mode = "danger-full-access"`,
capped output) to work around known Codex subagent stall-detection bugs.
Mounted into Codex by the host launcher only when the host `~/.codex`
does not already define `memory-bank-analyzer.toml`.

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

Primary file: `ai-cli-container`

Also consider:

- `README.md`
- `memory_bank/architecture.md`

### “I need to install another tool in the image.”

Primary file: `Dockerfile`

Also consider:

- whether the tool belongs under `dev` or root ownership
- whether host config for that tool also needs mounting in `ai-cli-container`
- whether `README.md` should expose it

### “I need to change the default Claude invocation.”

Primary file: `start-ai-cli`

Also consider whether the behavior should differ for arbitrary commands versus
interactive Claude startup.

### “I need to support a new launcher alias.”

Primary files:

- `Makefile`
- `ai-cli-container`
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
- `ai-cli-container`: short, but defines most host/container integration behavior
- `Makefile`: short, but part of the supported UX

Agents should not mistake file length for low importance.
