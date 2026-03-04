IMAGE := claude-code-dev:latest
PLATFORM ?= linux/$(shell uname -m | sed 's/x86_64/amd64/' | sed 's/aarch64/arm64/')

.PHONY: build test lint install clean

build:
	docker build --platform $(PLATFORM) -t $(IMAGE) .

test:
	cd container-plugin && python3 -m pytest test_pdb_mcp_server.py -v --tb=short
	python3 -m pytest test_docker_socket_proxy.py -v --tb=short \
		--cov=docker_socket_proxy --cov-report=term-missing --cov-fail-under=100

lint:
	cd container-plugin && python3 -m py_compile pdb_mcp_server.py
	python3 -m py_compile docker_socket_proxy.py

install: build
	install -m 755 claude-container /usr/local/bin/claude-container

clean:
	docker rmi $(IMAGE) 2>/dev/null || true
