# Persistence: Files In Any Format, And No Server Database

Last Updated: 2026-10-29
Last Verified: 2026-09-04

**Data addresses.** A name in backticks below is a LOGICAL name, not a path. Resolve it with `python scripts/data-path.py path <name>` before the file operation and use what it prints.

Always-active rule. This workspace is built on files, runs no server database, and uses SQLite as its database. Operator directive, 2026-08-21.

1. **Any file format is permitted:** Markdown, JSON, JSONL, YAML, XML, CSV, plain text, whatever fits the data. A JSON, YAML or JSONL file is not a database.
2. **In-process stores are permitted.** SQLite first, because the workspace already runs on it; LanceDB, DuckDB, Kuzu and their kin add no daemon and no port, so propose them on their merits.
3. **A server database is a rejection, not a trade-off.** Named: Postgres, Oracle, MySQL, Microsoft SQL Server, Redis, Qdrant, Neo4j, Elasticsearch, **and their kin**: the category is the rule and the names are examples, so MongoDB, Cassandra, ClickHouse and MinIO are refused without being listed. When a tool, library or design needs one, say so plainly and propose the file or SQLite form. Do not weigh it, pilot it, or add it "just for now".
4. **Prefer the stores that already exist.** `index.db` under `runtime:memory-index/cache` in the data overlay, `.memory-index-code/index.db`, `.codegraph/codegraph.db` and the Action Queue are the precedent. A new index is a new table or a new store file, never a new service.
5. **State the storage choice when you propose anything that persists**, and confirm in the completion line that nothing new listens on a port, e.g. `Persistence: one SQLite store, no daemon.`

Configuration is out of scope: a settings file stays whatever format its consumer expects. The two carve-outs, why each was a correction, the stores a new index should join, and the full validation step: `reference/persistence-detail.md`. Rationale: `ARCHITECTURE.md` § 4 under registry name `spec-docs`.
