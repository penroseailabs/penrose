---
name: google-maps
description: Google Maps by deep link: search a place or category, directions, hours and reviews.
packages: com.google.android.apps.maps
apps: Google Maps, Maps
---

- Search near the phone's location: `open_link(uri="geo:0,0?q=<words>")` (for example `geo:0,0?q=pharmacy`).
  A specific place: `geo:0,0?q=<name>+<area>`.
- Directions: `open_link(uri="https://www.google.com/maps/dir/?api=1&destination=<place>&travelmode=driving")`
  (`walking`, `transit`, `bicycling`). Navigation from where the phone is: `open_link(uri="google.navigation:q=<place>")`
  opens the route to it (the route card, "Finding best route…").
- A search result list is a sheet over the map; each place is a numbered element. Tapping one shows its
  card: open or closed now, hours, rating, reviews, phone, address. Swipe the card up for more.
- The map itself is a canvas: the screen text cannot read it. Work from the result sheet and the place
  card; request_screenshot only when the position on the map is the point.
- Location permission: grant it when asked (the phone's location is the user's real one).
