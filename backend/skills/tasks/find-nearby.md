---
name: find-nearby
description: "near me", "around here", "open now", "downtown", "the closest X": a place or service around the phone's location, from Google Maps, with distance and open-now status.
---

The user wants a place or service near where they are: a pharmacy open now, somewhere for dinner
tonight, the closest cash machine, a good coffee place nearby. Where they are is `phone_location` (the
phone's GPS fix) and what time it is comes from `locale`'s time zone: never ask for either, and never let
a city from a memory or an earlier task stand in for the phone.

Plan: one question step in Google Maps (the google-maps skill), with the point and the time in its
instructions ("the user is at lat 30.27, lng -97.74, downtown Austin; it is 9:40 pm local time").

- The step: `open_link(uri="geo:0,0?q=<category or dish>")` searches around the phone. Read the result
  sheet (name, rating, distance, open or closed) and the cards of the two or three best fits; for "open
  now" read the hours line on the card, not the list's badge alone. `done_when`: "Noted name, distance,
  open-now status and address of up to three places near the phone's location that fit."
- The agent has a few minutes: three places well read beat ten half read. A partial list is reported
  as partial, not searched again.
- If Maps shows no position after a moment (wait_for its result once), the fallback is a Google search
  in Chrome for "<category> open now <area>" with the area named from the coordinates in the brief; the
  subagent never sets or mocks the phone's location, and reports which source the answer came from.
- Filters the request implies go in the instructions: "open now", a cuisine, "within walking distance"
  (under about a mile), a price level, "takes reservations".
- The final answer: the places with distance, open-until time, address and the reason each fits; and,
  for "near me" asked late at night, a plain line when nothing suitable is open.
