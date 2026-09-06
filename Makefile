.PHONY: setup test up up-prebuilt pull down logs

# Local-dev only: seed .env from the example so `make up` has something to read.
# A real edge node is provisioned with install.sh (one-shot bootstrap for a
# clean Pi — clones, enrolls with C2, pulls prebuilt images):
#   curl -fsSL https://git.vpn.cusano.net/logan/node-26/raw/tag/v1/install.sh | sudo bash -s -- --help
setup:
	@test -f .env || cp .env.example .env
	@echo ".env ready — edit it, then 'make up' (local build) or 'make up-prebuilt'"

# Run pytest inside the running edge-node container.
# Requires: docker compose up (or at least the edge-node image built).
test:
	docker compose run --no-deps --rm edge-node pytest -v

# Build all images locally and start (dev mode with local code mounted).
up:
	docker compose up -d --build

# Pull pre-built images from the registry and start (no local build).
# Requires IMAGE_REGISTRY, DOCKER_ORG, DOCKER_REPO set in .env.
up-prebuilt:
	docker compose pull
	docker compose up --no-build -d

pull:
	docker compose pull

# Helper to pull the latest git commits and rebuild/restart the dev stack.
update-git:
	git pull
	docker compose build
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f edge-node
