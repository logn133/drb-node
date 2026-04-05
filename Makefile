.PHONY: setup test up down logs

setup:
	@[ -f .env ] && echo ".env already exists, skipping." || (cp .env.example .env && echo "Created .env — fill in your values before running 'make up'.")

# Run pytest inside the running edge-node container.
# Requires: docker compose up (or at least the edge-node image built).
test:
	docker compose run --no-deps --rm edge-node pytest -v

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f edge-node
