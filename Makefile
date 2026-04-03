IMAGE := claude-code-dev:latest
PROXY_TEST_IMAGE := docker-socket-proxy-test:latest
PLATFORM ?= linux/$(shell uname -m | sed 's/x86_64/amd64/' | sed 's/aarch64/arm64/')
INSTALL_DIR_DISPLAY := ~/.local/bin
INSTALL_DIR := $(HOME)/.local/bin
SUPPORT_DIR_DISPLAY := ~/.local/share/ai-cli-container
SUPPORT_DIR := $(HOME)/.local/share/ai-cli-container

.PHONY: build test install clean

build:
	docker build --platform $(PLATFORM) -t $(IMAGE) .

update:
	docker build --no-cache --platform $(PLATFORM) -t $(IMAGE) .

test:
	docker build --platform $(PLATFORM) -t $(PROXY_TEST_IMAGE) docker-socket-proxy
	docker run --rm $(PROXY_TEST_IMAGE)

install: build
	mkdir -p "$(INSTALL_DIR)"
	mkdir -p "$(SUPPORT_DIR)/codex-agents"
	install -m 755 ai-cli-container "$(INSTALL_DIR)/claude-container"
	ln -f "$(INSTALL_DIR)/claude-container" "$(INSTALL_DIR)/codex-container"
	ln -f "$(INSTALL_DIR)/claude-container" "$(INSTALL_DIR)/gemini-container"
	install -m 644 container-plugin/codex-agents/*.toml "$(SUPPORT_DIR)/codex-agents/"
	@echo "$(PATH)" | tr ":" "\n" | grep -Fqx -e "$(INSTALL_DIR)" -e "$(INSTALL_DIR_DISPLAY)" || { \
		echo "Add $(INSTALL_DIR_DISPLAY) to your PATH if you want to run these scripts by name."; \
	}
	@echo "Installed support assets to $(SUPPORT_DIR_DISPLAY)."
	@echo You can \"make update\" to rebuild the container later with current versions of the CLIs.

clean:
	docker rmi $(IMAGE) 2>/dev/null || true
	docker rmi $(PROXY_TEST_IMAGE) 2>/dev/null || true
