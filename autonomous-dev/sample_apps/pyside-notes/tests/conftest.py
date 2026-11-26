import pytest

from notes_app.storage import NoteStore
from notes_app.window import MainWindow


def pytest_configure(config):
    config.addinivalue_line("markers", "qt: pytest-qt widget and workflow tests (offscreen platform)")
    config.addinivalue_line("markers", "gui: real mouse/keyboard tests with PyAutoGUI (needs a desktop session)")
    config.addinivalue_line("markers", "acceptance: acceptance tests")


@pytest.fixture
def store(tmp_path):
    return NoteStore(tmp_path / "data")


@pytest.fixture
def window(qtbot, store):
    win = MainWindow(store, confirm_delete=False)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    return win
