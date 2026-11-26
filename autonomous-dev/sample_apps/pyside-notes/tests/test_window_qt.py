import pytest
from PySide6.QtCore import Qt

pytestmark = pytest.mark.qt


def test_window_created_with_widgets(window):
    assert window.windowTitle() == "Notes"
    assert window.title.isVisible() and window.body.isVisible()
    assert not window.delete_button.isEnabled()  # nothing selected yet


def test_create_note_workflow(qtbot, window, store):
    qtbot.keyClicks(window.title, "Meeting")
    window.body.setPlainText("agenda")
    qtbot.mouseClick(window.save_button, Qt.MouseButton.LeftButton)
    assert window.list.count() == 1
    assert store.notes[0].title == "Meeting"
    assert window.delete_button.isEnabled()


def test_empty_title_shows_validation_message(qtbot, window):
    qtbot.mouseClick(window.save_button, Qt.MouseButton.LeftButton)
    assert "Cannot save" in window.error.text()
    assert window.list.count() == 0


def test_edit_and_delete_note(qtbot, window, store):
    store.add("Old title")
    window.refresh()
    window.list.setCurrentRow(0)
    window.title.setText("New title")
    qtbot.mouseClick(window.save_button, Qt.MouseButton.LeftButton)
    assert store.notes[0].title == "New title"
    qtbot.mouseClick(window.delete_button, Qt.MouseButton.LeftButton)
    assert store.notes == [] and window.list.count() == 0


def test_search_filters_list(qtbot, window, store):
    store.add("Alpha")
    store.add("Beta")
    window.refresh()
    qtbot.keyClicks(window.search, "alp")
    assert window.list.count() == 1


def test_save_shortcut(qtbot, window, store):
    qtbot.keyClicks(window.title, "Shortcut")
    qtbot.keyClick(window.title, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    assert [n.title for n in store.notes] == ["Shortcut"]


def test_minimum_size_and_resize(window):
    window.resize(200, 100)
    assert window.width() >= 640 and window.height() >= 420
    window.resize(1000, 700)
    assert window.width() == 1000
