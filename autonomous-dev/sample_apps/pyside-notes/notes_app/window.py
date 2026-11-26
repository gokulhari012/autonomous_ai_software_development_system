"""Main window of the Notes application."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .storage import NoteStore


class MainWindow(QMainWindow):
    def __init__(self, store: NoteStore, confirm_delete: bool = True):
        super().__init__()
        self.store = store
        self.confirm_delete = confirm_delete
        self.setWindowTitle("Notes")
        self.setMinimumSize(640, 420)

        self.search = QLineEdit(placeholderText="Search notes")
        self.search.setObjectName("search")
        self.list = QListWidget()
        self.list.setObjectName("note_list")
        self.title = QLineEdit(placeholderText="Title")
        self.title.setObjectName("title")
        self.body = QPlainTextEdit()
        self.body.setObjectName("body")
        self.error = QLabel("")
        self.error.setObjectName("error")
        self.error.setStyleSheet("color: #b00020")
        self.new_button = QPushButton("New")
        self.save_button = QPushButton("Save")
        self.delete_button = QPushButton("Delete")
        for button, name in ((self.new_button, "new"), (self.save_button, "save"), (self.delete_button, "delete")):
            button.setObjectName(name)

        left = QVBoxLayout()
        left.addWidget(self.search)
        left.addWidget(self.list)
        right = QVBoxLayout()
        right.addWidget(self.title)
        right.addWidget(self.body)
        right.addWidget(self.error)
        buttons = QHBoxLayout()
        for button in (self.new_button, self.save_button, self.delete_button):
            buttons.addWidget(button)
        right.addLayout(buttons)
        root = QHBoxLayout()
        root.addLayout(left, 1)
        root.addLayout(right, 2)
        container = QWidget()
        container.setLayout(root)
        self.setCentralWidget(container)

        save_action = QAction("Save", self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self.save_note)
        self.addAction(save_action)

        self.new_button.clicked.connect(self.new_note)
        self.save_button.clicked.connect(self.save_note)
        self.delete_button.clicked.connect(self.delete_note)
        self.search.textChanged.connect(self.refresh)
        self.list.currentItemChanged.connect(self.show_selected)
        self.current_id: str | None = None
        self.refresh()
        self._update_buttons()

    def refresh(self) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for note in self.store.search(self.search.text()):
            item = QListWidgetItem(note.title)
            item.setData(Qt.ItemDataRole.UserRole, note.id)
            self.list.addItem(item)
            if note.id == self.current_id:
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)

    def show_selected(self, item: QListWidgetItem | None) -> None:
        if item is None:
            return
        note = self.store.get(item.data(Qt.ItemDataRole.UserRole))
        self.current_id = note.id
        self.title.setText(note.title)
        self.body.setPlainText(note.body)
        self.error.clear()
        self._update_buttons()

    def new_note(self) -> None:
        self.current_id = None
        self.list.clearSelection()
        self.title.clear()
        self.body.clear()
        self.error.clear()
        self.title.setFocus()
        self._update_buttons()

    def save_note(self) -> None:
        try:
            if self.current_id is None:
                note = self.store.add(self.title.text(), self.body.toPlainText())
                self.current_id = note.id
            else:
                self.store.update(self.current_id, self.title.text(), self.body.toPlainText())
        except ValueError as exc:
            self.error.setText(f"Cannot save: {exc}")
            return
        self.error.clear()
        self.refresh()
        self._update_buttons()

    def delete_note(self) -> None:
        if self.current_id is None:
            return
        if self.confirm_delete:
            answer = QMessageBox.question(self, "Delete note", "Delete this note?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.store.delete(self.current_id)
        self.new_note()
        self.refresh()

    def _update_buttons(self) -> None:
        self.delete_button.setEnabled(self.current_id is not None)
