# Repository Overview

## Purpose

This repository packages a portable Docker-based coding environment aimed at
agentic coding tools. It is not an application container for a specific product
or service. It is a developer tool that gives an AI coding CLI a controlled
workspace with:

- language-model CLIs installed inside the image
- host repositories mounted into the container
- user credentials and config mounted selectively from the host
- Docker access mediated through a policy-enforcing proxy
- a small Claude plugin bundle that adds subagents and a Python MCP server

The current README presents the repo primarily as a Claude-oriented container,
but the implementation also supports Codex and Gemini through launcher aliases.

## What Is In Scope

- Building and updating the development image
- Launching Claude, Codex, or Gemini inside the container
- Passing host auth/configuration through to the correct in-container location
- Constraining Docker access so the coding agent cannot start obviously unsafe
  containers
- Shipping Claude plugin assets in the image
- Shipping a Python MCP server for Pdb-based debugging inside Docker Compose
  services

## What Is Not In Scope

- Orchestrating multi-container apps for this repo itself
- Running a persistent service in production
- Managing an application framework or business logic
- Providing a general plugin system across all supported CLIs

The repo contains no application source tree beyond the tooling itself. Agents
should not look for web routes, API handlers, or business-domain models here.

## Current Top-Level Structure

- `README.md`: User-facing setup and usage documentation
- `Makefile`: Build, update, test, install, and clean entry points
- `Dockerfile`: Multi-stage image build and installed toolchain
- `claude-container`: Host launcher script used directly and via hard links
- `~/.local/share/ai-cli-container/`: Host-installed launcher support assets
  created by `make install` (inference from `Makefile`)
- `start-ai-cli`: Container entrypoint that starts the proxy and execs the tool
- `container-plugin/`: Claude plugin assets, including subagent prompts and the
  Pdb MCP server plus tests
- `docker-socket-proxy/`: Docker API policy proxy and its tests

## Design Priorities

The implementation suggests these priorities, in order:

1. Safe-enough Docker access for AI tools without giving direct daemon control
2. Convenient reuse of host credentials and config
3. One launcher UX that can front multiple coding CLIs
4. Small, inspectable Python/Bash code instead of heavy orchestration
5. Test coverage around the riskier Python components

## Important Product Behavior

### Launcher selection is name-based

`claude-container` is the real script. `codex-container` and `gemini-container`
are expected to be hard links pointing to the same file. The script uses its own
basename to decide which CLI flavor to launch and which credentials/config
directories to mount. `make install` also places host-side support assets under
`~/.local/share/ai-cli-container/` for launcher features that need a stable host
path.

### The current working directory is the main writable mount

The launcher mounts the host current working directory to the same absolute path
inside the container. That same directory becomes:

- the container working directory
- `ALLOWED_RW_BASE` for the Docker proxy

Its parent directory becomes `ALLOWED_MOUNT_BASE`, meaning sibling paths under
the same parent may be mounted read-only by containers started from inside the
coding container.

### Docker access is intentionally filtered

The coding agent inside the container does not talk to the host Docker socket
directly. The host socket is mounted to `/var/run/docker-real.sock`, then the
entrypoint starts a Python proxy that listens on `/var/run/docker.sock` and
validates `POST /containers/create` requests.

### Claude gets extra plugin support

Only the default Claude entrypoint path wires in `--plugin-dir
/home/dev/container-plugin` automatically. Codex and Gemini are launched
directly by the host launcher when selected, not through the `claude` wrapper
path in `start-ai-cli`.

## Maintenance Reality

This is a small repo with a low file count, so broad refactors are feasible.
However, several files are behaviorally coupled:

- `README.md`, `Makefile`, and `claude-container` must agree on install and
  usage behavior
- `Dockerfile` and `start-ai-cli` must agree on installed paths and runtime
  user assumptions
- `docker_socket_proxy.py` and its tests are tightly paired; policy changes
  should come with tests
- `container-plugin/pdb_mcp_server.py` and its tests are tightly paired; MCP
  API changes should come with tests

Any doc or code change that alters user-facing commands should usually update
`README.md` as well as the memory bank.
