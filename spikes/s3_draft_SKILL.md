---
name: notes-create-highlighted-checklist
description: Create an Apple Note with a highlighted line and a checklist
version: 0.1.0
author: hermes-recorder (draft from a 40 s demonstration)
license: MIT
metadata:
  hermes:
    tags: [notes, macos, recorded]
    related_skills: [hermes-recorder:replay-recorded]
---

# Create a highlighted checklist note

## When to Use
- The user asks to start a new note in Apple Notes with a title, a highlighted line and a checklist.

## Inputs
| Name | Description | Example from the recording |
| --- | --- | --- |
| `title` | First line of the note (becomes its title) | "This is a Test Note" |
| `highlight_text` | Line to highlight | "asdasdoijs" |
| `checklist_items` | Lines to turn into checklist items | "blah blah" |

## Steps
1. Open **Notes** (Dock).
2. Click the **New Note** button in the toolbar.
3. Type `title`, press Return twice, then type `highlight_text`.
4. Select `highlight_text`, click **Format**, then the **Highlight** checkbox.
5. Move to the end, press Return twice, and type `checklist_items`.
6. Select those lines, click **Format**, then choose a paragraph style *(the recording did not capture which one; ask)*.
7. Click **Checklist** in the toolbar.

## Approval points
None: nothing is sent, paid for, deleted or published.

## Verify
- A new note whose title is `title` appears at the top of the list, and the note count went up by 1.
- `highlight_text` is highlighted; `checklist_items` show checkboxes.

## Edge cases
- Which paragraph style did you pick in step 6 (Title, Heading, Body…)?
- Should the note go in a specific folder or account (the recording used "All iCloud")?
- If a note with the same title exists, create another or append to it?

## Recorded from
2026-11-05, Notes (com.apple.Notes), 40 s, no narration.
