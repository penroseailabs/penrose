---
name: youtube
description: YouTube by deep link: search, a video or channel by URL, what the player screen hides.
packages: com.google.android.youtube
apps: YouTube
---

- Search: `open_link(uri="https://www.youtube.com/results?search_query=<words>")`. A video:
  `https://www.youtube.com/watch?v=<id>`; a channel: `https://www.youtube.com/@<handle>`.
- Results are numbered elements with title, channel, views and age in their text: read them off the list.
  Opening a video plays it at once; the title, channel and description are below the player (swipe up).
- The player is a video surface: the screen text cannot read it. Do not request screenshots of it.
- What a video *says* is read with `youtube_transcript` (video id or link, optional start and end), in
  seconds, without playing it: use it before anything slower. Only if it fails: under the description tap
  "...more", then "Show transcript", and read the list (read_page). The transcript panel can be switched
  between languages with its drop-down.
- A Premium or sign-in promo sheet shows on first open: Skip or No thanks.
