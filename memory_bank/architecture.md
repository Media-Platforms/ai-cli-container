# Architecture

## High-Level Model

There are three execution layers:

1. Host machine
2. Development container
3. Child containers optionally started from inside the development container

The host launcher is responsible for creating layer 2. The Docker socket proxy
exists to constrain layer 3.

## Startup Flow

### 1. Host-side launcher

`claude-container` runs on the host and performs these steps:

1. Detect which tool flavor is requested from `basename "$0"`.
2. Fail early if Docker is unavailable or the image is missing.
3. Source `AI_CLI_GITHUB_TOKEN` from the environment or macOS Keychain when
   available so `gh` can authenticate inside the container.
4. For Codex, source `OPENAI_API_KEY` from the environment or macOS Keychain if
   the command being launched is the Codex CLI itself.
4. Build a set of bind mounts based on the current directory and which optional
   host config paths exist.
5. Run `docker run --rm -it ...` using the current directory as both the
   working directory and the primary writable bind mount.

### 2. Container entrypoint

The image entrypoint is `start-ai-cli`. It starts as root and:

1. Defines a PATH that preserves `/home/dev/.local/bin` when using `sudo`.
2. Checks whether `/var/run/docker-real.sock` exists.
3. If present, locks down that real socket to mode `700`.
4. Starts `docker-socket-proxy` in the background so unprivileged tooling can
   access Docker via the filtered socket at `/var/run/docker.sock`.
5. If the first CLI arg is a non-flag token, treats the command as an arbitrary
   process to exec as the `dev` user.
6. Otherwise, launches `claude` as `dev` with `--plugin-dir
   /home/dev/container-plugin` and `--dangerously-skip-permissions`, plus a
   default model if no explicit `--model` was supplied.

The distinction in step 5 is important. The entrypoint only wraps Claude when
the command begins with flags or is empty. Commands like `bash`, `aws`, or
`codex` bypass Claude-specific argument injection.

### 3. Optional nested Docker usage

Tools running inside the development container may invoke `docker` or `docker
compose`. Those requests go to the local proxy socket, which validates container
creation requests before forwarding them to the host daemon.

## Component Interaction

### `Dockerfile`

Builds a `node:bookworm-slim`-based runtime image with:

- AWS CLI copied from a separate builder stage
- Docker CLI and Compose plugin
- GitHub CLI
- Python 3 plus `mcp`
- Claude Code installed for the `dev` user
- Codex CLI installed globally with npm
- Gemini CLI installed globally with npm
- bundled plugin assets copied to `/home/dev/container-plugin`

The image switches between `root` and `dev` during build so install steps happen
under the right user context.

### `claude-container`

Owns host concerns:

- launcher UX
- runtime environment pass-through
- host config mounting
- current directory and parent-directory access policy
- flavor-specific setup for Claude/Codex/Gemini

### `start-ai-cli`

Owns in-container concerns:

- protecting the real Docker socket from the unprivileged user
- starting the proxy
- dropping privileges to `dev`
- deciding whether to run Claude or an arbitrary command

### `docker-socket-proxy/docker_socket_proxy.py`

Owns Docker API filtering. It behaves as a Unix-socket HTTP proxy and only
inspects one risk-sensitive class of requests deeply: container creation. It
currently rejects:

- privileged containers
- blocked Linux capabilities
- host PID namespace
- host network mode
- bind mounts outside `ALLOWED_MOUNT_BASE`
- writable bind mounts outside `ALLOWED_RW_BASE`

It supports both `Binds` and `Mounts` forms inside Docker create payloads.

### `container-plugin/`

Owns Claude-specific extras shipped in the image:

- subagent prompt definitions under `container-plugin/agents/`
- `pdb_mcp_server.py`, a stdio MCP server for interactive Pdb sessions launched
  with `docker compose run`

The existing `memory-bank-analyzer` prompt explicitly expects a `memory_bank`
directory in the current repo, which is why adding that directory is useful here
and in downstream repos using this container.

## Security and Trust Boundaries

### Boundary 1: Host to development container

The launcher mounts selected host files into the container. This is already a
high-trust boundary. Changes to mount lists or environment pass-through should
be treated as security-sensitive because they alter what secrets and host state
the in-container tool can access.

### Boundary 2: Development container to host Docker daemon

This is the most sensitive boundary in the repo. The proxy exists to reduce the
blast radius of allowing an AI tool to use Docker. Changes here require a clear
threat-model justification and tests.

### Boundary 3: Root to `dev` inside the container

The entrypoint starts as root only long enough to secure the real socket and
start the proxy. Normal tool execution happens as `dev`. Any change that causes
more work to happen as root should be scrutinized.

## Architectural Constraints

- The launcher assumes Docker is available on the host and that the built image
  tag is `claude-code-dev:latest`.
- The entrypoint assumes the proxy binary is available at
  `/usr/local/bin/docker-socket-proxy`.
- The proxy policy assumes the host launcher provides accurate
  `ALLOWED_MOUNT_BASE` and `ALLOWED_RW_BASE` values.
- The Pdb MCP server assumes Docker Compose is available inside the dev
  container.
- The launcher currently assumes a macOS host for keychain-based auth fallback
  (`AI_CLI_GITHUB_TOKEN` and `OPENAI_API_KEY`), because it shells out to
  `security find-generic-password`.

That last point matters for portability changes. If a task involves Linux-host
support for Codex auth fallback, the host launcher is the primary edit surface.
