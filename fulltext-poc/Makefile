.PHONY: up down bootstrap probe hello logs reindex backfill reset lint

up:
	docker compose up -d --build

down:
	docker compose down

bootstrap:
	docker compose run --rm --no-deps ingest-api python -m ftpoc.cli bootstrap

probe:
	@set -e; \
	out=$$(mktemp); \
	docker compose run --rm --no-deps ingest-api python -m ftpoc.cli s3-probe | tee "$$out"; \
	url=$$(grep '^PRESIGNED_URL:' "$$out" | sed 's/^PRESIGNED_URL: //'); \
	if [ -z "$$url" ]; then \
		echo "6      Presigned GET URL (host curl)              FAIL   no PRESIGNED_URL line emitted"; \
	else \
		code=$$(curl -s -o /dev/null -w '%{http_code}' "$$url"); \
		if [ "$$code" = "200" ]; then \
			echo "6b     Presigned GET URL fetched from HOST via curl PASS   HTTP $$code"; \
		else \
			echo "6b     Presigned GET URL fetched from HOST via curl FAIL   HTTP $$code"; \
		fi; \
	fi; \
	rm -f "$$out"

hello:
	docker compose run --rm --no-deps --user root -v "$$(pwd)/scripts:/app/scripts" ingest-api \
		python scripts/make-hello-pdf.py scripts/hello-smlouva.pdf
	@bash scripts/smoke-test.sh scripts/hello-smlouva.pdf

logs:
	docker compose logs -f

reindex:
	docker compose run --rm --no-deps ingest-api python -m ftpoc.cli reindex

backfill:
	docker compose run --rm --no-deps ingest-api python -m ftpoc.cli backfill

reset:
	@echo "This deletes ALL volumes (rustfs-data, opensearch-data, postgres-data)."
	@read -p "Type 'yes' to continue: " confirm && [ "$$confirm" = "yes" ] || (echo "Aborted"; exit 1)
	docker compose down -v

lint:
	docker compose run --rm --no-deps --user root ingest-api \
		sh -c "pip install -e .[dev] --quiet --root-user-action=ignore && ruff check src && mypy src"
