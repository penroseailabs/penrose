---
name: reservations
description: Restaurant tables by region: Google Maps "Reserve a table" first, then OpenTable, Resy, Yelp (US), TheFork, Quandoo (Europe, Australia), Zomato and EazyDiner (India); the apps, because the sites block this browser; booking is the consequential step.
packages: com.opentable, com.resy.android, com.tocktix, com.yelp.android, com.lafourchette.lafourchette, de.quandoo.android.consumerapp, com.application.zomato, com.easydiner
apps: OpenTable, Resy, Tock, Yelp, TheFork, The Fork, Quandoo, Zomato dining, EazyDiner, restaurant reservation, book a table, table
---

- Start in Google Maps, not on a booking site: `open_link(uri="geo:0,0?q=<cuisine or dish> restaurant")`
  searches around the phone's location ("downtown", "near me" mean that). A place's card shows its
  rating, hours and, where it takes online bookings, a "Reserve a table" button that lists the free
  times for a date and party size across the platforms it uses. Read two or three cards and report the
  places with a slot at the asked time; that answers "which places have a table".
- OpenTable, Resy, Tock and Yelp often block the emulator's Chrome (Access Denied, a bot check, 403): one
  blocked page is final for that host, never retried or reached through a proxy or mirror. Their apps
  work: `install_app` by package, search the city and date there. The apps hold the user's account and
  are where the booking itself happens.
- Which platform by region: US and Canada OpenTable, Resy (big cities), Tock (tasting menus), Yelp;
  UK OpenTable, TheFork, Quandoo; France, Spain, Italy TheFork; Germany, Austria, Switzerland Quandoo,
  OpenTable; Australia TheFork, Quandoo; India Zomato dining, EazyDiner; Mexico and Latin America
  OpenTable; elsewhere the place's own site or phone number from its Maps card.
- A booking is the consequential step: date, time, party size and the name on the booking are checked
  on the confirmation screen before the final tap, never confirmed twice, and the confirmation reference
  goes in the facts. A platform that asks for a name, phone number or email the brief does not have:
  report `needs_input` from that form; do not guess, and do not use the emulator's placeholder number.
- Many platforms hold a card for no-shows: entering card details is a payment detail only the user
  gives (`ask_for_password_or_code`), never typed from memory.
