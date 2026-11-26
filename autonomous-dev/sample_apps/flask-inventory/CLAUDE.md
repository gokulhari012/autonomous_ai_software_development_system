# Inventory Management - conventions

- Application factory: `inventory.create_app()`; blueprints `api` (JSON under `/api`) and `views` (HTML).
- Database: SQLite via `inventory/db.py` (`get_db()`); schema in `db.SCHEMA` (use `CREATE TABLE IF NOT EXISTS`).
  The database location comes from the `DATABASE_URL` environment variable (`sqlite:///path`).
- Data access lives in repository modules (`inventory/repository.py`); always use parameterised SQL.
- Validation raises `ValidationError(errors: dict)`; duplicates raise `DuplicateError`.
- Templates extend `base.html`; use labelled inputs and ARIA roles so tests can use `get_by_label` / `get_by_role`.
- Tests: pytest; API tests use the `client` fixture; browser tests are marked `e2e` and use relative URLs.
