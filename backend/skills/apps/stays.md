---
name: stays
description: Airbnb, Booking.com, Expedia, Hotels.com, Vrbo and Agoda: a dated search in a city by deep link, a listing's total and cancellation terms, instant book vs request.
packages: com.airbnb.android, com.booking, com.expedia.bookings, com.hcom.android, com.vrbo.android, com.agoda.mobile.consumer
apps: Airbnb, Booking.com, Booking, Expedia, Hotels.com, Vrbo, Agoda, hotels, stays
---

- Airbnb: `open_link(uri="https://www.airbnb.com/s/<city>/homes?checkin=<YYYY-MM-DD>&checkout=<YYYY-MM-DD>&adults=<n>")`.
  Booking.com: `open_link(uri="https://www.booking.com/searchresults.html?ss=<city>&checkin=<YYYY-MM-DD>&checkout=<YYYY-MM-DD>&group_adults=<n>&no_rooms=1")`.
  Expedia, Hotels.com, Vrbo and Agoda: their search form (city, dates, guests).
- Results show a nightly price; the total with cleaning and service fees and taxes is on the listing
  page near the Reserve or Book button: report the total, never the nightly figure alone. US listings
  may add a "resort fee" at the property: read the fine print under the price.
- A listing's cancellation policy is a line near the price ("Free cancellation before <date>",
  "Non-refundable"): read it and put it in the facts before any booking.
- Airbnb: "Reserve" on an "Instant Book" listing books at once; otherwise it sends a request the host
  must accept (the user then waits). Booking.com: "Reserve" then guest details then "Complete booking".
  Either final button is the consequential tap; the confirmation code goes in the facts.
- Map views are a canvas: filter and sort in the list instead.
