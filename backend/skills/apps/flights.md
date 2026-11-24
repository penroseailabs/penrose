---
name: flights
description: Flights and trains anywhere: compare on Google Flights by URL in Chrome; book in the user's booking app (Expedia, Kayak, Skyscanner, Hopper; MakeMyTrip, Cleartrip, Ixigo in India) or the airline's; trains on Amtrak, Trainline, DB Navigator, IRCTC.
packages: com.expedia.bookings, com.kayak.android, net.skyscanner.android.main, com.hopper.mountainview.play, com.makemytrip, com.cleartrip.android, com.ixigo, com.amtrak.rider, com.thetrainline, de.hafas.android.db, cris.org.in.prs.ima
apps: Google Flights, Expedia, Kayak, Skyscanner, Hopper, MakeMyTrip, MMT, Cleartrip, Ixigo, Amtrak, Trainline, DB Navigator, IRCTC, flights, trains
---

- To find and compare: `open_link(uri="https://www.google.com/travel/flights?q=flights+from+<from>+to+<to>+on+<YYYY-MM-DD>", package="com.android.chrome")`
  (add `+return+<YYYY-MM-DD>` for a round trip; city names or IATA codes; prices in the locale's
  currency). Google Flights lists airlines, times, stops and prices and is read-only: nothing can be
  booked there by mistake. Kayak by link: `https://www.kayak.com/flights/<FROM>-<TO>/<YYYY-MM-DD>`.
- To book: the app the user uses (memories, installed apps), else the airline's own app or site, which
  has no OTA fees. Searches are by form: from, to, date, passengers, class. The fare on the list may
  rise on the next screen ("fare updated"): report the final one.
- Booking screens in order: fare family (choose the plain one unless the brief says), traveller details
  (names exactly as on the ID, date of birth, gender, passport for international; from the brief or
  memories, never guessed), add-ons (insurance, seats, bags, meals, "price freeze": all declined
  unless asked), contact details, payment. The final Pay or Book button is the consequential tap; the
  booking reference or PNR on the confirmation screen goes in the report's facts.
- Trains: Amtrak (US), Trainline (UK and Europe), DB Navigator (Germany) search by form and book like a
  flight. IRCTC (India) needs the user's IRCTC login and shows a captcha at login and at booking; a
  captcha is the user's to solve on the live phone: report needs_input saying so.
