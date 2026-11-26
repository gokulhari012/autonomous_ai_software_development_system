"""Entry point: python -m notes_app.main"""

from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from .storage import NoteStore
from .window import MainWindow


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = QApplication(sys.argv)
    window = MainWindow(NoteStore())
    window.show()
    logging.getLogger("notes").info("Notes application started")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
