# nahrada-fn

- [`fulltext-poc/`](fulltext-poc/README.md) - the fulltext search pipeline (FileNet replacement PoC): RustFS, PostgreSQL (also the full-text search index), Tika behind a docker-compose stack.
- [`ansible/`](ansible/README.md) - Ansible playbook that deploys the `fulltext-poc` docker-compose stack to a remote host.
- [`docs/anatomie-fulltext-poc.html`](docs/anatomie-fulltext-poc.html) - component diagram (custom code vs. configuration) and production-readiness recommendations.
- [`docs/opensearch-alternativy.md`](docs/opensearch-alternativy.md) - the research/decision record behind replacing OpenSearch with PostgreSQL full-text search (this branch). The `main` branch keeps the OpenSearch-based version.
