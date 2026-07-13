.PHONY: setup test up up-prebuilt pull down logs

setup:
	@bash setup.sh

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
