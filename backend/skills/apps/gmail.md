---
name: gmail
description: Gmail: search mail by words, read a thread, reply, compose by mailto link, what sending means.
packages: com.google.android.gm
apps: Gmail, email, mail
---

- The Google account is signed in already. Search: the box at the top; Gmail's operators work there
  (`from:`, `subject:`, `newer_than:7d`, `has:attachment`, `OR` in capitals, `( )`, `-` to leave out).
  Search for the kind of email the brief needs, not the store's name alone: an order's confirmation is
  `from:<store> subject:(order OR ordered OR "your order" OR receipt)`, where it is now `<item>
  subject:(shipped OR delivered OR "out for delivery")`; `-category:promotions` leaves out cart reminders
  and offers.
- Compose with prefilled fields: `open_link(uri="mailto:<address>?subject=<subject>&body=<text>")`
  (URL-encoded). The Send button is the consequential tap; sending cannot be undone past the few-second
  Undo toast.
- A thread shows the latest message expanded; older ones are collapsed rows (tap to expand). Attachments
  are chips at the bottom of a message; tapping one opens a viewer, which may be a canvas.
- Reply and Reply all are at the bottom of a message; check which one the brief wants before writing.
- Reading is enough for most tasks ("find the booking confirmation", "what did X say"): note the facts
  and report; do not archive or mark as read unless asked.
- Deleting is blocked on the phone as a safeguard against prompt injection: Delete, Delete forever,
  Discard drafts and Empty trash or spam are refused (marked `blocked`), and a swipe set to delete does
  nothing. If the user asks to delete mail, say they can delete it themselves; archiving still works.
