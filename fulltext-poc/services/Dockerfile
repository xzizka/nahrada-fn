FROM python:3.12-slim

# fonts-dejavu-core: DejaVuSans.ttf, embedded by scripts/make-hello-pdf.py so
# the PoC's Czech diacritics survive PDF generation (reportlab's built-in
# fonts are Latin-1 only and silently drop them).
RUN apt-get update \
    && apt-get install --no-install-recommends -y fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 ftpoc \
    && useradd --uid 10001 --gid ftpoc --shell /usr/sbin/nologin --no-create-home ftpoc

WORKDIR /app

COPY services/pyproject.toml ./
COPY services/src ./src

RUN pip install --no-cache-dir .

COPY scripts ./scripts
COPY opensearch/index-template.json ./opensearch/index-template.json

RUN chown -R ftpoc:ftpoc /app
USER ftpoc

# Actual process is selected per-service via `command:` in docker-compose.yml
# (ingest-api, worker, scanner, search-api all share this one image).
CMD ["python", "-m", "ftpoc.cli", "--help"]
