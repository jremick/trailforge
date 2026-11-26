.PHONY: help up down logs ps stats lint test test-unit test-integration migrate seed-corridor web-check fixtures

help:
	@echo "TrailForge developer commands:"
	@echo "  make up               - Build and start PostGIS, API, worker, and web"
	@echo "  make down             - Stop all containers"
	@echo "  make ps               - Show compose service status"
	@echo "  make stats            - Show container memory/CPU snapshot"
	@echo "  make lint             - Run ruff lint and format checks"
	@echo "  make test-unit        - Run offline unit tests via uv (no DB needed)"
	@echo "  make test-integration - Run offline PostGIS integration tests inside API container"
	@echo "  make test             - Run lint + unit tests + integration tests"
	@echo "  make migrate          - Run Alembic migrations inside API container"
	@echo "  make seed-corridor    - Ingest Alps corridor, sync closures & build PMTiles"
	@echo "  make web-check        - Run web typecheck, build, and Vitest suite"
	@echo "  make fixtures         - Regenerate deterministic synthetic VIC/NSW fixtures"

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f

ps:
	docker compose ps

stats:
	docker stats --no-stream

lint:
	uv run ruff check src tests migrations

test-unit:
	uv run pytest -m "not live and not integration"

test-integration:
	docker compose exec -T api pytest -m "not live"

test: lint test-unit test-integration

migrate:
	docker compose exec -T api alembic upgrade head

seed-corridor:
	docker compose exec -T api python -m trailforge.ingestion.pipeline --fixtures tests/fixtures
	docker compose exec -T api python -m trailforge.feeds.cli --fixtures tests/fixtures/closures_sample.json
	docker compose exec -T api python -m trailforge.tiles.cli --output-dir /data/tiles --dem tests/fixtures/dem_alps_corridor.tif

web-check:
	cd web && npm run typecheck && npm run build && npm test

fixtures:
	uv run python tests/fixtures/generate_fixtures.py
