"""Briefings: what penrose has for the user from the notifications of the apps they chose, and the "For you"
list every screen starts from.

A notification no chat waits for (no watch, no open task in its conversation: app/notifications.py) used to stay
in the log. For an app the user chose to be briefed on it now joins a briefing: the notifications of one
conversation of one app, looked at once as a whole, and offered to the user in penrose's words.

    policy.py    the user's own choices: the apps to brief on, the conversations they muted. No model decides it.
    groups.py    which briefing a notification joins, and a briefing's notifications as the agents read them
    triage.py    sorting a briefing once it has gone quiet: noise, a line in the list, or worth a briefing
                 (the tool-less TRIAGE agent), in the worker
    dossier.py   what the orchestrator reads of a briefing: what arrived, and every earlier conversation on the
                 same matter, never one
    actions.py   what the user does: choose an app, open a briefing (its chat, and the orchestrator on what can
                 be done about it), dismiss one, mute its conversation
    feed.py      the "For you" list: the briefings, the chats that wait for the user's answer, what else came in

They depend on each other in that order, and only groups.py is known to the notification intake. A briefing's
task reads and prepares, changes nothing, and ends by offering what it can do next (ask_user's `options`): the
user's answer makes it an ordinary task (agent_prompts/orchestrator.md, Briefings).
"""
