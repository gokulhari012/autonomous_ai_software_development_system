# Notes - conventions

- PySide6 only (no PyQt). Entry point `python -m notes_app.main`.
- `notes_app/storage.py` owns persistence (`NoteStore`, atomic JSON writes, data dir from `APP_DATA_DIR`).
- `notes_app/window.py` holds the `MainWindow`; give widgets an `objectName` so tests can find them.
- Dialogs must be injectable/skippable in tests (see `confirm_delete`).
- Tests: pytest + pytest-qt; widget tests are marked `qt`; real-input tests are marked `gui`.
