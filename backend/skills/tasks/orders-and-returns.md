---
name: orders-and-returns
description: Where is my order, reorder my usual, return or cancel an item, talk to support: the orders page of the app is the place, and the app is the one the order was made in.
---

- "Where is my order" / "has it shipped": one step in the app named, else the store the order emails
  name (`look_up` the item's shipping emails as a Gmail search: the email-search skill), else the app the last orders memory names,
  else each shopping app installed, one step at a time, stopping at the first that shows the order. The orders page (the app skill gives its link) lists orders with status; open the newest. Report
  item, status, expected date, and the tracking line.
- "Reorder" / "my usual": the orders page has Reorder or Buy again on past orders; the step rebuilds the
  cart from the last order and stops before paying unless the request authorises the order, with the
  cart in `done_when` for verification.
- Cancel or return: both are consequential (a cancel may be final, a return starts a pickup, drop-off or shipping label). The step
  finds the order, reads the options and their terms (refund method, pickup or drop-off date), and reports
  needs_input with them unless the request already fixed the choice; then a second step does it and
  reports the reference.
- Support chat: in the app's Help section; the step states the problem in a line, reads the bot's
  options, picks the one that matches, and reports what support said and any ticket number. It never
  agrees to a replacement, refund amount or charge the user did not ask for.
