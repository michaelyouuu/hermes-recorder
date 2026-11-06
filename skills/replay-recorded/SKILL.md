---
name: replay-recorded
description: Run a skill recorded with hermes-recorder safely
version: 0.1.0
author: Michael Mi
license: MIT
metadata:
  hermes:
    tags: [recorder, replay, follow-up, meetings]
    related_skills: []
---

# Replaying a recorded skill

## When to Use
- Running any skill whose frontmatter says `author: recorded with hermes-recorder` (it relates to this skill).
- The user says "follow up on my last meeting", "do the follow-up", or "file the action items" after a call.

## Follow up on a meeting
1. Find the meeting: call `recorder_notes` (empty query, `days: 1`) and pick the newest note with `kind: meeting`,
   or the one the user names. Read its **Action items** (`- [ ] task (owner, due)`) and **Decisions**.
2. Find the recorded skill for the follow-up (for example `file-meeting-action-items-in-linear`) in your skills.
3. Show the user which action items you'll handle and the input values you'll use for each. Skip items owned by
   someone else unless the user says otherwise.
4. Run the skill once per item (below).

## Running a recorded skill
- Fill every `{input}` from the user's request or the meeting note. If a value is missing, ask; never guess
  names, dates or amounts.
- Prefer the browser tools for web apps and `computer_use` for native apps. Find elements by the visible names the
  steps give ("New issue button", "Title field"), not by position. If the page looks different from the steps,
  stop and describe what you see.
- **Approval points are mandatory.** Before any step marked "ask the user to approve first", or anything that
  creates, sends, submits, pays, deletes or publishes, show what you're about to do and wait for an explicit yes.
- If a step marks a password, let the user type it. Never type or store secrets.
- After the last step, run the skill's **Verify** checks and report what you saw. On failure, stop and say which
  step failed; don't improvise around it.
- If the skill's **Edge cases** still say "unanswered", ask those questions before running it the first time and
  offer to update the skill with the answers.
