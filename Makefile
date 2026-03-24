# Convenience commands for development and Docker testing

PY ?= python3
PIP ?= pip3
IMAGE ?= ghcr.io/$(USER)/discord-bear-bot:latest
CONTAINER ?= bearbot
VENV ?= .venv

.PHONY: help
help:
	@echo "Targets:"
	@echo "  venv           - Create virtual environment"
	@echo "  install        - Install dependencies into venv"
	@echo "  dev-run        - Run bot locally (requires DISCORD_TOKEN in env)"
	@echo "  docker-build   - Build Docker image"
	@echo "  docker-run     - Run container with volume and restart policy"
	@echo "  docker-stop    - Stop and remove container"
	@echo "  docker-clean   - Remove image and dangling images"

$(VENV)/bin/activate:
	$(PY) -m venv $(VENV)

venv: $(VENV)/bin/activate

install: venv
	. $(VENV)/bin/activate && $(PIP) install -U pip && $(PIP) install -r requirements.txt

# Usage: make dev-run DISCORD_TOKEN=your_token_here
.PHONY: dev-run
dev-run:
	@if [ -z "$(DISCORD_TOKEN)" ]; then echo "ERROR: Set DISCORD_TOKEN=..."; exit 1; fi
	. $(VENV)/bin/activate 2>/dev/null || true; \
	export DISCORD_TOKEN=$(DISCORD_TOKEN); \
	python -m src.bot

.PHONY: docker-build
docker-build:
	docker build -t $(IMAGE) .

# Usage: make docker-run DISCORD_TOKEN=your_token_here
.PHONY: docker-run
docker-run:
	@if [ -z "$(DISCORD_TOKEN)" ]; then echo "ERROR: Set DISCORD_TOKEN=..."; exit 1; fi
	-docker rm -f $(CONTAINER) 2>/dev/null || true
	docker run -d \
		--name $(CONTAINER) \
		--restart unless-stopped \
		-e DISCORD_TOKEN=$(DISCORD_TOKEN) \
		-v discord-bear-bot-data:/data \
		$(IMAGE)

.PHONY: docker-stop
docker-stop:
	-docker rm -f $(CONTAINER) 2>/dev/null || true

.PHONY: docker-clean
docker-clean:
	-docker rmi $(IMAGE) 2>/dev/null || true
	-docker image prune -f