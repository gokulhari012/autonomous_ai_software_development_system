# Notes (sample PySide6 desktop application)

```bash
pip install -r requirements.txt
python -m notes_app.main
QT_QPA_PLATFORM=offscreen python -m pytest -m "not gui"
```

Data is stored in `$APP_DATA_DIR/notes.json` (default `~/.pyside-notes`).
