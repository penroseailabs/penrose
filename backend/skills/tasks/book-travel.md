---
name: book-travel
description: Flights, trains, stays and rides: compare read-only first, report options with totals and terms, book only the chosen one as its own verified step.
---

Travel is high-stakes: wrong date, wrong name, non-refundable. The plan keeps looking and booking apart.

- Flights: step 1 in Chrome on Google Flights (the flights skill) for the dates and route; report the
  three best options by price and duration with airline, times, stops and price. Step 2, after the user
  picks (or the request fixed it), in the booking app the user uses, else the airline's: find that flight, enter the travellers from
  `context`, decline add-ons, stop at the payment screen if the user has not authorised paying, else pay
  within the limit and report the PNR.
- Trains: the region's rail app (Amtrak, Trainline, DB Navigator, IRCTC; the flights skill) for
  options and booking. IRCTC needs the user at the captcha: the plan says they will be asked to solve
  it on the phone.
- Stays: one step in the app the user uses (Airbnb, Booking.com, else both, one step each) with the dated
  search link from the stays skill; report three options with the total for the stay, rating, area, and
  the cancellation terms. Booking is its own step, after the choice.
- Rides: one step in Uber (or the user's app): set the drop, read the fares, and book only when the
  request says "book" (else report the fares); the ride details go in the final answer.
- Traveller details (names as on ID, dates of birth, passport numbers for international flights) come
  from `user` memories or an `ask_user` before the booking step, never from guesses; passport numbers are
  asked for privately. The final answer: what was booked, for whom, the dates, the total, the
  reference, and the cancellation terms.
