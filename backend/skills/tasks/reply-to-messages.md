---
name: reply-to-messages
description: "Reply to my messages" / "see who wrote and answer for me": triage new messages in a messaging app, send replies in the user's voice within the rules given, report every message sent.
---

The user hands over their chats for a while. What makes this safe is the rules in `context`: who may be
answered, what may be said, what must wait for the user.

- Plan one step per app named (WhatsApp, Telegram, Signal, Messenger, Instagram, Messages, LinkedIn; "my messages" with no
  app means WhatsApp plus any app whose message notifications are in `conversation`). The step: list unread chats, read
  each, decide whether it needs a reply, reply where the rules allow, and report every chat (who, what
  they said in a line, what was sent or why not).
- `context` carries the rules, written out: the user's name and how they write (short, no emoji, which
  languages, from `user` memories); what to say about the user's absence if anything ("I'm travelling,
  back Monday"); who gets a reply (everyone, only contacts, a named list); what never gets a reply
  (requests for money, codes, passwords, documents; anything legal or medical; anything the user would
  decide themselves: a meeting time, a price, a yes to an invitation). Those are reported, not answered.
- Group chats get no reply unless the request names the group. Unknown numbers get none unless the
  request says so.
- A reply is one message, in the user's voice, that answers what was asked and nothing more; no promises
  on the user's behalf. The subagent puts recipient and text in the facts; the final answer lists them
  all, and separately the chats that wait for the user.
- Replying while the user is away for hours needs the task to run again on new messages; a single run
  answers what is there now and says so. (A standing "watch my chats" task is not something the plan can
  set up today.)
