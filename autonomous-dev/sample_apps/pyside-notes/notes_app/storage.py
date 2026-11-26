"""JSON file storage for notes."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("notes.storage")


@dataclass
class Note:
    title: str
    body: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


def default_data_dir() -> Path:
    return Path(os.environ.get("APP_DATA_DIR") or Path.home() / ".pyside-notes")


class NoteStore:
    def __init__(self, data_dir: Path | None = None):
        self.data_dir = Path(data_dir or default_data_dir())
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "notes.json"
        self.notes: list[Note] = self._load()

    def _load(self) -> list[Note]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return [Note(**item) for item in raw]
        except (json.JSONDecodeError, TypeError) as exc:
            log.error("notes file is corrupt (%s); starting empty and keeping a backup", exc)
            self.path.replace(self.path.with_suffix(".corrupt.json"))
            return []

    def save(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump([asdict(n) for n in self.notes], handle, indent=2)
        os.replace(tmp, self.path)  # atomic write

    def add(self, title: str, body: str = "") -> Note:
        title = title.strip()
        if not title:
            raise ValueError("title must not be empty")
        note = Note(title=title, body=body)
        self.notes.append(note)
        self.save()
        return note

    def update(self, note_id: str, title: str, body: str) -> Note:
        if not title.strip():
            raise ValueError("title must not be empty")
        note = self.get(note_id)
        note.title, note.body = title.strip(), body
        self.save()
        return note

    def delete(self, note_id: str) -> None:
        self.notes = [n for n in self.notes if n.id != note_id]
        self.save()

    def get(self, note_id: str) -> Note:
        for note in self.notes:
            if note.id == note_id:
                return note
        raise KeyError(note_id)

    def search(self, text: str) -> list[Note]:
        needle = text.strip().lower()
        if not needle:
            return list(self.notes)
        return [n for n in self.notes if needle in n.title.lower() or needle in n.body.lower()]
