# Inventory Management (sample Flask application)

A small Flask + SQLite application used to exercise the autonomous development controller.

```bash
pip install -r requirements.txt
flask --app "inventory:create_app()" run
python -m pytest -m "not e2e"                      # unit / API / database tests
python -m pytest -m e2e --base-url http://127.0.0.1:5000   # Playwright tests against a running server
```

Configuration: `DATABASE_URL=sqlite:///path/to/db`, `SECRET_KEY`.
