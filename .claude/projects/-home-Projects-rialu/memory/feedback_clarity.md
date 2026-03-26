---
name: feedback-be-clear-about-sudo
description: Be explicit when asking the user to run commands vs doing it yourself
type: feedback
---

When a command requires sudo or can't be run from Claude Code's sandbox, say "Please run:" or "You'll need to run:" — not "Let me restart" or "Let me do X" which implies Claude is doing it.

**Why:** Todd pointed out that "Let me restart the agent:" sounds like Claude is restarting it, when actually it requires the user to run a script with sudo.
**How to apply:** Always be explicit about who is performing the action, especially for commands that require sudo or a separate terminal.
