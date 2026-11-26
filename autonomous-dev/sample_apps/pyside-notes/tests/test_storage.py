import pytest

from notes_app.storage import NoteStore


def test_add_persists_across_instances(tmp_path):
    store = NoteStore(tmp_path)
    note = store.add("Shopping", "milk")
    reloaded = NoteStore(tmp_path)
    assert reloaded.get(note.id).body == "milk"


def test_empty_title_rejected(tmp_path):
    with pytest.raises(ValueError):
        NoteStore(tmp_path).add("   ")


def test_update_and_delete(tmp_path):
    store = NoteStore(tmp_path)
    note = store.add("A")
    store.update(note.id, "B", "body")
    assert store.get(note.id).title == "B"
    store.delete(note.id)
    assert store.notes == []


def test_search_matches_title_and_body(tmp_path):
    store = NoteStore(tmp_path)
    store.add("Groceries", "eggs")
    store.add("Work", "buy eggs for the team")
    store.add("Travel", "passport")
    assert len(store.search("eggs")) == 2
    assert len(store.search("")) == 3


def test_corrupt_file_is_backed_up(tmp_path):
    (tmp_path / "notes.json").write_text("{not json", encoding="utf-8")
    store = NoteStore(tmp_path)
    assert store.notes == []
    assert (tmp_path / "notes.corrupt.json").exists()
