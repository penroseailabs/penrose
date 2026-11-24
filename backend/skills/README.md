# Skills

Authored knowledge the agents start from. Short Markdown files with a front matter; read on every model
call, so an edit applies at once. See `app/skills.py` for how they reach the agents.

- `apps/<slug>.md`: how one app works. Front matter: `name`, `description`, `packages` (Android package
  IDs, comma-separated), `apps` (names the orchestrator may write for it). The subagent and the verifier
  working in that app read the body in full, ahead of the memories learned on earlier visits.
- `tasks/<slug>.md`: a recipe for a kind of request that keeps coming back. Front matter: `name`,
  `description`. The orchestrator reads every recipe in full when it plans a new task.
- `tools/<slug>.md`: how to use one of the agents' own tools well (what it searches, how to word a call,
  worked examples). Front matter: `name`, `description`, `tools` (tool names). Every agent that has one
  of those tools reads it in full at the end of its instructions. The tool's own description says what
  it does and when; the skill says how, and grows with what the tool can reach (the home app's sources)
  without a backend restart. When to call it is the agent's prompt. `tools/sign-in.md` is the sign-in
  playbook (the identity providers' screens, what to do and say at each, the second-factor rule): keyed
  to `ask_login`, `ask_for_password_or_code` and `ask_user`, so the subagent and the orchestrator both read it.

What belongs in an app skill: deep links (the biggest saver: one `open_link` instead of five turns of
tapping), the login method, where the things tasks need live (cart, orders, search, settings), sheets and
promos that get in the way and how to dismiss them, a flow that does not work and what does. What does
not: tap-by-tap sequences (apps change; the screen wins), the user's data, anything the agents already
know about phones in general.

A lesson the memory agent keeps saving for one app is a line for that app's skill.

Skills are drafts until a run confirms them: a deep link or a screen described here may be wrong for
an app's current version. The screen wins; the memory agent's app notes record what actually worked,
and those notes are the edits to bring back here.
