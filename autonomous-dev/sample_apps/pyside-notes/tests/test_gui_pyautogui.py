"""Real mouse/keyboard test with PyAutoGUI. Runs only in a real desktop session when the
orchestrator authorises real input automation; otherwise the layer is reported BLOCKED."""

import pytest

pytestmark = pytest.mark.gui


def test_type_title_with_real_keyboard(qtbot, window, store):
    pyautogui = pytest.importorskip("pyautogui")
    window.raise_()
    window.activateWindow()
    qtbot.waitActive(window)
    centre = window.title.mapToGlobal(window.title.rect().center())
    pyautogui.click(centre.x(), centre.y())
    pyautogui.write("Typed by PyAutoGUI", interval=0.01)
    qtbot.waitUntil(lambda: window.title.text() == "Typed by PyAutoGUI", timeout=5000)
    window.save_note()
    assert store.notes[0].title == "Typed by PyAutoGUI"
