# Testing And Validation

## Existing Validation Surfaces

The repo has two real test suites and one build-oriented validation path.

### Python tests for the Docker socket proxy

File:

- `docker-socket-proxy/test_docker_socket_proxy.py`

Coverage focus:

- path authorization
- bind and mount policy
- request-body validation
- header parsing and HTTP status parsing
- chunked forwarding and stream splicing
- request proxying and run-loop behavior

### Python tests for the Pdb MCP server

File:

- `container-plugin/test_pdb_mcp_server.py`

Coverage focus:

- session lifecycle
- prompt detection and buffered output handling
- command sending and stop semantics
- MCP tool handlers
- server tool registration

### Image/proxy smoke validation

`make test` builds the proxy test image from `docker-socket-proxy/` and runs it.
That validates the proxy suite in a containerized way. `make build` validates
that the main image still builds.

## Expected Validation By Change Type

### If you change `docker-socket-proxy/docker_socket_proxy.py`

Minimum expectation:

- run the proxy test suite, preferably through `make test`

Ideal expectation:

- also reason through security impact and ensure new negative cases are tested

### If you change `container-plugin/pdb_mcp_server.py`

Minimum expectation:

- run the Pdb MCP server tests, for example with `pytest
  container-plugin/test_pdb_mcp_server.py`

Ideal expectation:

- add focused tests for any new lifecycle or protocol behavior

### If you change `Dockerfile`, `ai-cli-container`, or `start-ai-cli`

Minimum expectation:

- perform at least one build or syntax-oriented validation relevant to the file

Examples:

- `docker build ...` for `Dockerfile`
- `bash -n ai-cli-container start-ai-cli` for shell changes
- a targeted manual smoke test if the environment allows it

Because these files define runtime behavior rather than pure library logic, some
validation is inevitably manual or environment-dependent.

### If you change docs only

Minimum expectation:

- verify the docs match the current code

No automated doc checks are currently present in the repo.

## Practical Commands

Common commands an agent can reach for:

```bash
make build
make test
python3 -m pytest container-plugin/test_pdb_mcp_server.py
python3 -m pytest docker-socket-proxy/test_docker_socket_proxy.py
bash -n ai-cli-container
bash -n start-ai-cli
```

If `pytest` is not installed in the local environment, a containerized path may
be needed, or the agent should report that validation could not be completed.

## Testing Philosophy

The Python components in this repo handle async subprocesses, stream parsing,
and security policy. Regressions in those areas are easy to miss by inspection
alone. Prefer narrow, explicit tests over broad optimistic manual claims.

For this repo, “validated” should usually mean one of:

- unit tests passed
- the main image built successfully
- shell syntax checked successfully
- a specific launcher/runtime path was exercised and observed

## Gaps To Keep In Mind

There is no dedicated automated suite for:

- host-side launcher behavior across platforms
- end-to-end invocation of Claude/Codex/Gemini inside the built image
- README correctness

When changing those areas, call out residual risk explicitly.
