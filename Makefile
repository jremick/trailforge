.PHONY: help up down logs ps stats lint test test-unit test-integration migrate seed-corridor bake-alps bake-blue-mountains web-check fixtures

TEST_DB ?= trailforge_test
BAKE_ARGS ?=

help:
	@echo "TrailForge developer commands:"
	@echo "  make up                  - Build and start PostGIS, API, worker, and web"
	@echo "  make down                - Stop all containers"
	@echo "  make ps                  - Show compose service status"
	@echo "  make stats               - Show container memory/CPU snapshot"
	@echo "  make lint                - Run ruff lint and format checks"
	@echo "  make test-unit           - Run offline unit tests via uv (no DB needed)"
	@echo "  make test-integration    - Run offline PostGIS integration tests on isolated $(TEST_DB) DB"
	@echo "  make test                - Run lint + unit tests + integration tests"
	@echo "  make migrate             - Run Alembic migrations inside API container"
	@echo "  make seed-corridor       - Ingest Alps corridor, closures, telemetry, live grades, tiles & offline pack"
	@echo "  make bake-alps           - Bake real Australian Alps corridor (pass BAKE_ARGS='--pbf ...')"
	@echo "  make bake-blue-mountains - Bake real Blue Mountains corridor (pass BAKE_ARGS='--pbf ...')"
	@echo "  make web-check           - Run web typecheck, build, and Vitest suite"
	@echo "  make fixtures            - Regenerate deterministic synthetic VIC/NSW fixtures"

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
	docker compose exec -T db sh -ec '\
		psql -U "$${POSTGRES_USER:-trailforge}" -d postgres -tc "SELECT 1 FROM pg_database WHERE datname = '\''$(TEST_DB)'\''" | grep -q 1 \
		|| psql -U "$${POSTGRES_USER:-trailforge}" -d postgres -c "CREATE DATABASE \"$(TEST_DB)\";"; \
		psql -U "$${POSTGRES_USER:-trailforge}" -d "$(TEST_DB)" -c "CREATE EXTENSION IF NOT EXISTS postgis;" \
	'
	docker compose exec -T api sh -ec '\
		TEST_URL="$${TEST_DATABASE_URL:-$${DATABASE_URL%/*}/$(TEST_DB)}"; \
		export DATABASE_URL="$$TEST_URL"; \
		export TEST_DATABASE_URL="$$TEST_URL"; \
		alembic upgrade head; \
		pytest -m "not live" \
	'

test: lint test-unit test-integration

migrate:
	docker compose exec -T api alembic upgrade head

seed-corridor:
	docker compose exec -T api python -m trailforge.ingestion.pipeline --fixtures tests/fixtures
	docker compose exec -T api python -m trailforge.feeds.cli --fixtures tests/fixtures/closures_sample.json
	docker compose exec -T api python -m trailforge.worker.seed_demo --gpx tests/fixtures/sample_trace_davies_plain.gpx
	docker compose exec -T api python -m trailforge.grading.cli --offline-rain-72h 18.5
	docker compose exec -T api python -m trailforge.tiles.cli --output-dir /data/tiles --dem tests/fixtures/dem_alps_corridor.tif
	docker compose exec -T api python -m trailforge.packs.cli --region australian-alps --output-dir /data/packs --dem tests/fixtures/dem_alps_corridor.tif

bake-alps:
	docker compose exec -T api python -W error::RuntimeWarning -m trailforge.ingestion.bake_cli \
		--region australian-alps --cache-dir /data/cache --build-tiles --build-pack $(BAKE_ARGS)

bake-blue-mountains:
	docker compose exec -T api python -W error::RuntimeWarning -m trailforge.ingestion.bake_cli \
		--region blue-mountains --cache-dir /data/cache --build-tiles --build-pack $(BAKE_ARGS)

web-check:
	cd web && npm run typecheck && npm run build && npm test

fixtures:
	uv run python tests/fixtures/generate_fixtures.py
