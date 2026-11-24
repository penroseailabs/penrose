---
name: reserve-a-table
description: "book a table for N at a <kind of> place <where> <when>": find the places with a slot (a question step from Google Maps), then book one as its own authorised step.
---

The user wants a restaurant table: a party size, a date and time, a kind of food, and a place that is
usually relative to them ("downtown", "near the office", "around here" bind to `phone_location`; a city
from a memory does not override it). The dates come from `locale`'s time zone ("this Saturday" is the
next Saturday from today there).

Plan: a question step that finds places with a free slot, then, only when the user clearly asked for the
booking to be made and a place is chosen, an action step that books it.

- Step 1 (Google Maps, kind question): search `geo:0,0?q=<cuisine> restaurant` (plus the area word the
  user gave), open the cards of two or three well-rated places and read "Reserve a table" for the date,
  time and party size: which platform, which times are free. `done_when`: "Noted up to three places with
  a table at or near the asked time, each with the platform and the free times." If a place shows no
  reserve button, its card's phone number and site are the answer for it. Three places is enough;
  report partial after the budget rather than widening the search.
- Do not open OpenTable, Resy, Tock or Yelp in Chrome: they block this browser (see the reservations
  skill). Their apps are for the booking step.
- Ask the user nothing before step 1 except a genuine ambiguity in the request. Which place to book is
  asked after step 1, with the options and times found, unless the user said "you pick" or the choice
  is clear. The booking name and phone number are asked only when step 2 reaches the form and reports
  `needs_input`.
- Step 2 (the platform's app, kind action): book the chosen place at the chosen time for the party
  size, with the user's name and phone from the conversation in the instructions once they gave them.
  It is consequential: `done_when` is the confirmation screen with a reference, and `verify_step` reads
  it back. Never book two places, never book without the user's clear go-ahead in `context`.
- The final answer: the booked place, time, party size and reference; or, for a question-only request,
  the places with free times and how each is booked.
