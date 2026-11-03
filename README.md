# Agentic Coding Dev Container

A portable Docker-based development environment for running
[Claude Code](https://claude.ai/claude-code),
[Codex](https://chatgpt.com/codex/), or
[Gemini](https://geminicli.com)
safely in YOLO mode. This container provides a consistent, isolated environment with all three
CLIs, AWS and GitHub CLIs, and Docker tooling pre-installed.

## Features

- **Claude Code CLI** - Latest version installed and ready to use
- **OpenAI Codex CLI** - Run `codex-container` to use Codex instead
- **Google Gemini CLI** - Run `gemini-container` to use Gemini instead
- **AWS CLI v2** - For AWS Bedrock integration and other AWS operations
- **Docker + Docker Compose** - Filtered Docker access via socket proxy
- **GitHub CLI (`gh`)** - Create pull requests, manage issues, and interact with GitHub
- **Python 3 + MCP** - Python debugging support with pre-configured Pdb MCP server
- **Non-root execution** - Runs as unprivileged `dev` user for security
- **Volume mounting** - Seamlessly access your host repositories
- **Configuration persistence** - Your Claude, Codex, Gemini, and AWS configs are mounted from host

## Prerequisites

- Docker installed on your host machine
- (Optional) AWS credentials configured in `~/.aws/` for Bedrock usage
- (Optional) Claude Code configuration in `~/.claude/` and `~/.claude.json`
- (Optional) GitHub CLI authenticated via `gh auth login`, `AI_CLI_GITHUB_TOKEN`, or macOS Keychain for PR/issue workflows

## Quick Start

Clone or download this repo, `cd` into it, and run `make install`. This installs `claude-container`, `codex-container`, and `gemini-container` into `~/.local/bin` as hard links to the same script, and installs launcher support assets under `~/.local/share/ai-cli-container/`. If `~/.local/bin` is not already on your `PATH`, the install step prints a reminder.

## Usage

### Basic usage

```bash
# Launch Claude Code in current directory
claude-container

# Pass additional arguments to Claude
claude-container --model opus

# Launch Codex CLI instead (--yolo is added automatically)
codex-container

# Launch Gemini CLI instead (--yolo is added automatically)
gemini-container

# Pass CLI flags straight through to the selected tool
codex-container --help
gemini-container --help

# Resume a Codex session
codex-container resume

# Run arbitrary commands in the container with any launcher
codex-container bash
gemini-container bash

# Show launcher help instead of CLI help
claude-container --container-help

# Run arbitrary commands in the container
claude-container bash
claude-container aws s3 ls
```

### Authentication

The launcher script mounts your config and auth token directories, so for Claude or Gemini you can
just SSO before starting the container.

For Codex, the script will export `OPENAI_API_KEY` from your environment, or from your macOS Keychain.
For GitHub CLI workflows, it will export `AI_CLI_GITHUB_TOKEN` from your environment, or from your macOS Keychain, as `GITHUB_TOKEN` inside the container.

### Choosing a Claude backend

`claude-container` can reach Claude through AWS Bedrock, through an
Anthropic-compatible gateway such as a LiteLLM proxy, or through the Anthropic
API with the credentials already in `~/.claude`. Set `AI_CLI_BACKEND` to
`bedrock`, `gateway`, or `anthropic` to pick one:

```bash
# AWS Bedrock, using ~/.aws credentials
AI_CLI_BACKEND=bedrock claude-container

# A LiteLLM (or other Anthropic-compatible) gateway
AI_CLI_BACKEND=gateway claude-container
```

When `AI_CLI_BACKEND` is unset the launcher infers the backend: Bedrock if
`CLAUDE_CODE_USE_BEDROCK` is set, otherwise the gateway if `ANTHROPIC_BASE_URL`
or `AI_CLI_ANTHROPIC_API_KEY` is set, otherwise the Anthropic API.

### Configuring a LiteLLM gateway

A LiteLLM gateway for Claude Code must expose Anthropic-compatible endpoints,
including `POST /v1/messages`. The container uses a virtual key from the host
and never requires a key to be written into the image or copied into a project.

#### 1. Store the virtual key on the host

The launcher looks for a gateway key in this order:

1. `AI_CLI_ANTHROPIC_API_KEY` in the host environment
2. `ANTHROPIC_API_KEY` in the host environment
3. macOS Keychain item `AI_CLI_ANTHROPIC_API_KEY`
4. macOS Keychain item `ai-gateway-virtual-key`

Use one Keychain item rather than an exported key where possible. If your host
`apiKeyHelper` already reads `ai-gateway-virtual-key`, store the container key
there too so the host and container use the same virtual key. Use Keychain
Access to create or update a generic password item with:

- **Service:** `ai-gateway-virtual-key`
- **Account:** your macOS username
- **Password:** the LiteLLM virtual key

A stale exported key wins over either Keychain item, so unset
`AI_CLI_ANTHROPIC_API_KEY` and `ANTHROPIC_API_KEY` when you intend to use the
Keychain.

The launcher passes the selected key to the container as
`AI_CLI_ANTHROPIC_API_KEY` and replaces any host `apiKeyHelper` with
`printenv AI_CLI_ANTHROPIC_API_KEY`. This makes Claude Code send the key both
as `x-api-key` and as a bearer token. Do not set `ANTHROPIC_AUTH_TOKEN` for
this configuration, and do not set both `ANTHROPIC_API_KEY` and
`ANTHROPIC_AUTH_TOKEN`: Claude Code treats those as conflicting credentials.

#### 2. Configure the base URL and model names

Put the LiteLLM URL and the model IDs exposed by the gateway in the host
`~/.claude/settings.json`. That directory is mounted into the container.

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://litellm.example.com",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
    "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5-5",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-5-5",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-5-5"
  }
}
```

Use the exact model IDs configured in LiteLLM, not assumed provider-specific
names. The three `ANTHROPIC_DEFAULT_*_MODEL` variables control the Opus,
Sonnet, and Haiku tiers respectively. The Haiku setting is also used for
background work, so it must identify a model that the gateway key can use.
`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1` enables model discovery, and
`CLAUDE_CODE_ATTRIBUTION_HEADER=0` disables Claude Code's attribution header;
the launcher supplies those values by default in gateway mode, but keeping them
in `settings.json` also applies them to host Claude Code.

A host `apiKeyHelper` is typically a macOS `security` command, which cannot
run in the Linux container. The launcher overrides it only for the container;
it does not modify `~/.claude/settings.json`.

#### 3. Validate access and launch

Set the backend explicitly. The launcher chooses a backend from the host
process environment and does not use the `env` block in `settings.json` for
backend selection:

```bash
AI_CLI_BACKEND=gateway claude-container -p "reply with ok"
```

Before launching, check that the virtual key can invoke the model names in your
settings. `GET /v1/models` is useful for discovering model IDs, but a gateway
may list models that the key is not authorized to invoke. This command sends a
small billable request and verifies actual access:

```bash
KEY="$(security find-generic-password -s ai-gateway-virtual-key -w)"
GATEWAY_URL="https://litellm.example.com"

printf 'x-api-key: %s\n' "$KEY" | curl -sS -H @- \
  -H 'content-type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  "$GATEWAY_URL/v1/messages" \
  -d '{"model":"claude-opus-5-5","max_tokens":5,"messages":[{"role":"user","content":"reply with ok"}]}'
```

A `403` response with `key_model_access_denied` means the key is valid but is
not allowed to use that model. Change the configured model IDs to models the
key can use, or ask the LiteLLM administrator to grant access. A `401` means
the key was rejected. Claude Code retries either error several times without
printing output, which can look like a hang.

To inspect a failed container request, write a debug log to the mounted working
directory, interrupt after the first few retries, then inspect the API errors:

```bash
AI_CLI_BACKEND=gateway claude-container \
  --debug-file ./claude-debug.log -p "reply with ok"
grep -E 'API error|dispatching|gatewayDiscovery' claude-debug.log
```

### Environment Variables

The launcher script passes through these environment variables from your host if set:

- **`AI_CLI_BACKEND`** - Which backend Claude Code uses: `bedrock`, `gateway`, or `anthropic`
- **`AI_CLI_ANTHROPIC_API_KEY`** - API key for the gateway backend (falls back to macOS Keychain if not set)
- **`ANTHROPIC_BASE_URL`** - Base URL for the gateway backend
- **`AWS_PROFILE`** - AWS profile to use (from `~/.aws/config`)
- **`AWS_REGION`** - AWS region for API calls
- **`CLAUDE_CODE_USE_BEDROCK`** - Set to `1` to use AWS Bedrock instead of Anthropic API
- **`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY`** - Set to `1` to list the gateway's models (defaults to `1` for the gateway backend)
- **`CLAUDE_CODE_ATTRIBUTION_HEADER`** - Set to `0` to drop Claude Code's attribution header (defaults to `0` for the gateway backend)
- **`AI_CLI_GITHUB_TOKEN`** - GitHub personal access token for `gh` CLI (passed as `GITHUB_TOKEN` inside the container, falls back to macOS Keychain if not set)
- **`GOOGLE_CLOUD_PROJECT`** - Google Cloud project ID for Gemini CLI Vertex usage
- **`GOOGLE_CLOUD_LOCATION`** - Google Cloud region for Gemini CLI Vertex usage
- **`OPENAI_API_KEY`** - OpenAI API key for Codex CLI (falls back to macOS Keychain if not set)

API keys and tokens are handed to `docker run` by name rather than by value, so their values do not show up in the host process list where any local user could read them with `ps`. They are still visible in `docker inspect` of the dev container, because Docker stores a container's environment in its spec.

Example:

```bash
# Set environment variables for your session
export AWS_PROFILE=myprofile
export AWS_REGION=us-west-2
export CLAUDE_CODE_USE_BEDROCK=1

# Launch container (will inherit the above settings)
claude-container
```

Or as a one-liner:

```bash
AWS_PROFILE=prod AWS_REGION=us-east-1 CLAUDE_CODE_USE_BEDROCK=1 claude-container
```

## What Gets Mounted

The launcher script automatically mounts:

| Host Path | Container Path | Purpose |
|-----------|---------------|---------|
| Current directory | Same path in container | Your working repository |
| `~/.aws` | `/home/dev/.aws` | AWS credentials and config |
| `~/.gitconfig` | `/home/dev/.gitconfig` | Git user identity and settings |
| `~/.claude` | `/home/dev/.claude` | Claude Code settings |
| `~/.claude.json` | `/home/dev/.claude.json` | Claude API key config |
| `~/.codex` | `/home/dev/.codex` | Codex CLI settings when using `codex-container` |
| `~/.gemini` | `/home/dev/.gemini` | Gemini CLI settings when using `gemini-container` |
| `~/.config/gcloud` | `/home/dev/.config/gcloud` | Google Cloud auth/config when using `gemini-container` |
| `~/.config/gh` | `/home/dev/.config/gh` | GitHub CLI auth tokens |
| `/var/run/docker.sock` | `/var/run/docker-real.sock` | Docker daemon (behind proxy) |

When the current directory contains a Docker Compose project, the launcher also
adds `<service>.docker` host aliases inside the dev container for each Compose
service name, all pointing at Docker's host gateway. This lets the dev
container reach ports published by that project's containers through a local
hostname like `voltron.docker`.

## Container Details

- **Base Image**: `debian:bookworm-slim`
- **Container Name**: `<tool>-<current-directory>-<launcher-pid>` (directory name is sanitized for Docker)
- **User**: `dev` (non-root, with no sudo rights — see [Docker Socket Proxy](#docker-socket-proxy))
- **Working Directory**: Mirrors your host's current directory
- **Default Command**: `claude --dangerously-skip-permissions --model us.anthropic.claude-opus-4-6-v1`

## Docker Socket Proxy

The container includes a security proxy that sits between the CLI's Docker client and the host Docker daemon. Sharing the host daemon otherwise means root on the host, because a container can be asked to mount `/` or to run privileged. The proxy answers only requests it recognizes and refuses everything else, so an API field it has never heard of is rejected rather than forwarded.

### What's Allowed

- Read requests (`GET` and `HEAD`) to every resource except plugins, swarm, services, tasks, nodes, configs and secrets
- Writes to a fixed list of endpoints covering containers, images, builds, networks and volumes — enough for `docker run`, `build`, `exec`, `cp`, `logs`, `compose up`, and pruning
- On `POST /containers/create`, every `HostConfig` field is either known to be harmless, checked individually, or required to be empty

### What's Blocked

- **Privileged containers** (`--privileged`) and **privileged exec** (`docker exec --privileged`)
- **Added capabilities** outside Docker's default set. This is an allowlist, so `SYS_ADMIN`, `SYS_PTRACE`, `BPF`, `ALL` and any capability added in a future Docker release are all rejected. Dropping capabilities is always allowed
- **Host namespaces** — `--pid=host`, `--network=host`, `--ipc=host`, `--uts=host`, `--userns=host`, `--cgroupns=host`
- **Joining another container's namespaces** — `--pid=container:x` and the equivalent for ipc, network and uts. A process in a shared PID namespace can read another container's mounts through `/proc/<pid>/root`
- **Weakened confinement** — `--security-opt apparmor=unconfined`, `--security-opt systempaths=unconfined`, `--security-opt label=disable`, and an empty `MaskedPaths` or `ReadonlyPaths`
- **Host devices** (`--device`), **GPU requests** (`--gpus`) and **alternative runtimes** (`--runtime`)
- **`--volumes-from`**, which inherits whatever mounts the named container has
- **Volume driver options that describe a bind** — `docker volume create --driver local -o type=none -o o=bind -o device=/` mounts any host path without naming it in a bind mount, as does the same thing spelled as a `Type=volume` mount's `VolumeOptions.DriverConfig.Options`. Third-party volume drivers are rejected for the same reason: the proxy cannot tell what they will mount. tmpfs volumes are allowed
- **Log drivers** other than `json-file`, `local`, `journald` and `none`, because a driver like `syslog` can be pointed at a host socket
- **Plugins, swarm services, configs, secrets and nodes**, whose endpoints install host-level code or run containers the proxy never sees
- **BuildKit builds**, which tunnel an arbitrary protocol over `/session` and `/grpc`. The classic builder is allowed, and is what this image's Docker CLI uses because it has no buildx plugin
- **`docker build --network=host` and `--security-opt`**, which are build-time versions of the container flags above

Requests are also rejected before reaching the daemon when they are framed ambiguously — both `Content-Length` and `Transfer-Encoding` set, or either header repeated — because hiding a second request inside the body of the first needs the proxy and the daemon to disagree about where the body ends.

### Mount Restrictions

Bind mounts are restricted based on your working directory. Given a working directory of `/home/user/projects/my-app`:

| Path | Mode | Result |
|------|------|--------|
| `/home/user/projects/my-app/src` | rw | Allowed |
| `/home/user/projects/sibling` | ro | Allowed |
| `/home/user/projects/sibling` | rw | **Rejected** |
| `/etc/shadow` | any | **Rejected** |

The proxy passes `ALLOWED_MOUNT_BASE` (parent of cwd) and `ALLOWED_RW_BASE` (cwd) to control these restrictions.

### How It Works

1. The host Docker socket is mounted as `/var/run/docker-real.sock` (not the usual path)
2. On container startup, the entrypoint locks down the real socket (`chmod 700`) so only root can access it
3. A Python proxy starts in the background, listening on `/var/run/docker.sock`
4. The `dev` user's Docker CLI talks to the proxy, which validates and forwards requests to the real daemon

The `dev` user has no sudo rights and is not in the sudo group, which is what keeps step 2 meaningful: with sudo, any process in the container could talk to `/var/run/docker-real.sock` directly and the proxy would be beside the point.

The proxy parses requests the way the daemon does, so that a request cannot mean one thing to each of them. It percent-decodes the path, strips the API version prefix so that `/containers/create` and `/v1.52/containers/create` are checked identically, matches JSON field names without regard to case because Go's JSON decoder does, and rejects a body that spells the same field twice rather than having to guess which spelling the daemon will use.

### What the Proxy Does Not Protect Against

- **Symlinks in mount paths.** A bind mount source is checked as written. A symlink under your working directory that points somewhere else is allowed, and the daemon resolves it on the host
- **Reading sibling directories.** `ALLOWED_MOUNT_BASE` is the parent of your working directory, so anything beside your project can be mounted read-only
- **Containers that already exist on the host.** The container can list them and can `exec`, `attach`, or write files into them with `docker cp`. Whatever those containers can reach, it can reach
- **The workspace mount itself.** Your working directory is mounted at the same path inside the container, so a nested container can be given the same write access the agent already has
- **Outbound network traffic**, which is not restricted at all
- **Secrets in the container's own spec.** The launcher passes API keys by name rather than by value, which keeps them out of the host process list, but `docker inspect` of the dev container still shows them
- **The host kernel.** This is an allowlist over one HTTP API, not a sandbox. If you need a real boundary, do not share the host daemon: run Docker rootless, or use Sysbox or a VM

Refusing the unrecognized cuts both ways. A Docker release that adds a new `HostConfig` field capable of an escape is already handled, because a field the proxy does not classify has to be empty. But a Docker release that adds a new *endpoint* is refused too, so a newly shipped client feature can fail with a 403 that has nothing to do with security. The proxy logs the reason it refused; the fix is to add the endpoint to `WRITE_RULES` in `docker-socket-proxy/docker_socket_proxy.py`, not to loosen the matching.

### Seccomp Policy

The container runs with `--security-opt seccomp=unconfined` because Codex's Rust sandbox uses bubblewrap, which requires unprivileged user namespaces (`unshare CLONE_NEWUSER`). Docker's default seccomp profile blocks this syscall. The Docker socket proxy still enforces container-creation policy, and the `dev` user has no way to reach the host Docker socket directly. The proxy also allows `--security-opt seccomp=...` on nested containers, since the dev container already runs with seccomp disabled and a child of it gains nothing new.

## Troubleshooting

### Installing packages inside the container

The `dev` user has no sudo rights, so `apt install` fails, and so does
`npm install -g` because the global module directory belongs to root. Add what
you need to the `Dockerfile` and run `make update`.

For a one-off package during a session, install it under `/home/dev`, which
`dev` owns:

```bash
pip install --user <package>          # already on PATH via ~/.local/bin
npm install --prefix ~/.local <package>
```

### A nested docker command fails with 403

The proxy refuses Docker API endpoints it does not recognize, so a command it has
never been asked for can fail even when it is harmless. The proxy logs the
reason on the dev container's stderr, for example
`rejected: endpoint not allowed: POST /containers/foo/bar`. See
[Docker Socket Proxy](#docker-socket-proxy).

### Docker socket permissions

If you encounter Docker permission errors, ensure your user is in the `docker` group on the host:

```bash
sudo usermod -aG docker $USER
# Log out and back in for changes to take effect
```

### Reaching nested Docker Compose apps from inside the dev container

Containers started with `docker compose` from inside the dev container run on
the host Docker daemon, so `127.0.0.1` inside the dev container refers to the
dev container itself, not the published port on the host.

If the current directory is a Compose project, the launcher maps each Compose
service name to the host gateway as `<service>.docker`. That means a service
named `voltron` can be reached directly like this:

```bash
curl http://voltron.docker/health
```

The aliases come from `docker compose config --services`, so they follow the
current project's Compose configuration instead of exposing a general-purpose
host alias.

### AWS credentials not found

Ensure your `~/.aws` directory exists and contains valid credentials:

```bash
aws configure
# or
aws configure --profile myprofile
```

### GitHub CLI not authenticated

There are two ways to authenticate `gh` inside the container:

**Option 1: Interactive login (recommended for personal use)**

```bash
# On your host machine
gh auth login
```

This stores credentials in `~/.config/gh/` which the container mounts automatically.

**Option 2: `AI_CLI_GITHUB_TOKEN` environment variable or macOS Keychain**

```bash
export AI_CLI_GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
claude-container
```

To store the same token in your macOS Keychain instead:

```bash
security add-generic-password -s AI_CLI_GITHUB_TOKEN -a github -w 'ghp_...'
```

Required token scopes:

- **Classic PAT**: `repo` scope
- **Fine-grained PAT**: "Contents: Read and write" + "Pull requests: Read and write" on target repos

See GitHub docs for creating tokens: [Managing your personal access tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)

For more on the GitHub CLI: [About GitHub CLI](https://docs.github.com/en/github-cli/github-cli/about-github-cli)

### Codex CLI not authenticating

`codex-container` pulls `OPENAI_API_KEY` from the environment first, then falls back to the macOS Keychain. To store the key in your Keychain:

```bash
security add-generic-password -s OPENAI_API_KEY -a openai -w 'sk-...'
```

Or export it directly:

```bash
export OPENAI_API_KEY=sk-...
codex-container
```

### Gemini CLI not authenticating

`gemini-container` mounts `~/.gemini` and `~/.config/gcloud` into the container so existing Gemini CLI and Google Cloud authentication can be reused there. For Vertex, it also passes through `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` when they are set on the host.

### Claude Code not authenticating

Run Claude Code once on your host to set up authentication, or manually place your API key in `~/.claude.json`:

```json
{
  "apiKey": "your-api-key-here"
}
```

### Claude Code not reaching the gateway

If `AI_CLI_BACKEND=gateway` requests go to `api.anthropic.com` instead of your
gateway, the launcher found no base URL: set `ANTHROPIC_BASE_URL` on the host or
add it to the `env` block of `~/.claude/settings.json`. The launcher warns when
it can find neither.

If the gateway rejects requests as unauthenticated, check which key the launcher
picked up: `AI_CLI_ANTHROPIC_API_KEY` and `ANTHROPIC_API_KEY` both win over the
Keychain entries, so a stale value exported in your shell will shadow a freshly
rotated key. The LiteLLM setup and model-access checks are in
[Configuring a LiteLLM gateway](#configuring-a-litellm-gateway).

## Container Plugin

The container includes a built-in plugin that provides additional subagents and MCP servers. This plugin **layers on top of** any configuration you have in your mounted `~/.claude/` directory without modifying it.

### How It Works

The container uses Claude Code's `--plugin-dir` flag to load `/home/dev/container-plugin`, which contains:

```
container-plugin/
├── .claude-plugin/
│   └── plugin.json           # Plugin manifest
├── pdb_mcp_server.py         # Pdb MCP server (loaded only when python-test-debugger is used)
└── agents/
    └── python-test-debugger.md  # Subagent definitions (includes inline MCP server config)
```

**Benefits of the plugin approach:**

- **Non-invasive** - Doesn't modify your `~/.claude/settings.json`
- **Layered** - Container tools add to (not replace) your existing config
- **Priority** - Your user-level agents take precedence over plugin agents if names conflict

### Included Subagents

#### python-test-debugger

A specialized agent for debugging failing Python tests interactively. Use it when:

- VCR/mocking doesn't seem to be working
- Tests fail after dependency updates
- Async/event loop errors occur
- Tests behave differently when run individually vs. in a suite

The agent has access to the `pdb` MCP server for interactive debugging.

#### memory-bank-analyzer

Extracts project context from `memory_bank/` or `memory-bank/` files (or both, if both exist) before starting implementation work. It runs **proactively** after you define a task to:

- Extract coding standards and style guidelines
- Identify architectural patterns and conventions
- Find project structure and organization principles
- Understand testing approaches and requirements

This ensures implementations follow your project's established patterns and standards.

**Claude** uses this as a subagent loaded via `--plugin-dir`. **Gemini** gets an equivalent subagent installed at container startup to `~/.gemini/agents/` (Markdown). For **Codex**, the launcher generates a temporary `AGENTS.override.md` that merges any existing host global instructions with the injected memory-bank rule, and stages a temporary `agents/` directory containing a bundled `memory-bank-analyzer.toml` fallback tuned for fast completion (`model_reasoning_effort = "low"`, `sandbox_mode = "danger-full-access"`, capped output) to work around known Codex subagent stall-detection bugs ([openai/codex#16900](https://github.com/openai/codex/issues/16900)).

### Included MCP Servers

#### pdb (Python Debugger)

`pdb_mcp_server.py` is embedded in the `container-plugin/` directory and is only started when the `python-test-debugger` skill is invoked — it does **not** run during normal Claude sessions. It provides:

- **`start_pdb_session`** - Start a Pdb session in a Docker Compose service
- **`send_pdb_command`** - Send commands (e.g., `n`, `s`, `p var`)
- **`stop_pdb_session`** - Stop the debugging session

### Adding Your Own Container Extensions

To add more subagents or MCP servers to the container:

1. Add subagent markdown files to `container-plugin/agents/`
2. To include an MCP server that loads only when a specific agent is used, place the server script in `container-plugin/` and define it inline in the agent's frontmatter:
   ```yaml
   mcpServers:
     my-server:
       command: python3
       args: ["/home/dev/container-plugin/my_server.py"]
   ```
3. Rebuild the container

Example subagent (`container-plugin/agents/my-agent.md`):

```markdown
---
name: my-agent
description: Description of when Claude should use this agent
tools: Read, Grep, Glob, Bash
model: inherit
---

You are a specialist in...
```
