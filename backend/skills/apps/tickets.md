---
name: tickets
description: Cinema, concert and event tickets by region: Fandango and Ticketmaster (US), Ticketmaster and Eventbrite (UK, Europe, Australia), BookMyShow (India); search by link, showtimes, seat maps, the booking screens.
packages: com.fandango, com.ticketmaster.mobile.android.na, com.eventbrite.attendee, com.bt.bms
apps: Fandango, Ticketmaster, Eventbrite, BookMyShow, BMS, movie tickets, event tickets, concert tickets
---

- Search links: Fandango `https://www.fandango.com/search?q=<title>`; Ticketmaster
  `https://www.ticketmaster.com/search?q=<words>` (`.co.uk`, `.com.au`, `.de` by country); Eventbrite
  `https://www.eventbrite.com/d/<country>--<city>/<words>/`; BookMyShow
  `https://in.bookmyshow.com/explore/movies-<city>` (and `events-<city>`). Each app asks for a city or
  location on first open: pick the user's.
- A film page lists dates, then cinemas with showtimes; an event page lists dates and venues. Tapping a
  showtime or date opens the seat map, which is a canvas: request_screenshot to read it, then tap seats
  by position (Ticketmaster also offers "best available" by price, which needs no map); the chosen
  seats and total appear in a bar below.
- After seats: a summary (seats, show, total with fees), then contact details, then payment; the Pay or
  Place Order button is the consequential tap. Food, insurance, donations and "fan club" add-ons are
  skipped. Ticketmaster puts a countdown on checkout: finish or report, do not restart the search.
- Showtimes are also one Google search away (`<film> showtimes <area>`); use the ticket app when the
  task books, or when the search gives no times.
