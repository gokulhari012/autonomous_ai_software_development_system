# Notes - requirements

## R1. Notes (implemented)
Create, edit, delete and search notes (title + body). Titles are required. Notes are saved to a JSON
file in the application data directory and survive restarts. Ctrl+S saves the current note.

## R2. Export notes to Markdown (NOT YET IMPLEMENTED)
- A toolbar/menu action "Export..." writes all notes to a single Markdown file chosen by the user.
- Each note becomes a level-2 heading (`## <title>`) followed by its body.
- Exporting with no notes shows an information message instead of writing an empty file.
- A failure to write the file (e.g. permission denied) shows an error message and does not crash.
