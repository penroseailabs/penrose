---
name: google-search
description: Web search in Chrome by URL: results, news, recent-only, and reading a result page.
packages: com.android.chrome
apps: Chrome, Google, Google Search, web search
---

Search by URL, never by typing into the Chrome address bar (the Google app is switched off on the phone):

- `open_link(uri="https://www.google.com/search?q=<words>", package="com.android.chrome")`: web results.
  Encode spaces as `+`.
- Add `&tbm=nws` for news results only; `&tbs=qdr:d` for the last day, `qdr:w` for the last week.
- The top of the page may carry an AI overview, a "People also ask" block, ads (marked Sponsored) and a
  consent or sign-in sheet. Dismiss any sheet with its Reject, No thanks or Close. Ads are not sources.
- Result links are numbered elements with an href: tap one to read the page, press_key back to return to
  the results. A result page may hold a cookie banner (decline) or a "continue in app" sheet (close).
- A reading task is done when you have what the step asks, noted with `note`; scroll a results page at
  most twice before changing the query.
- Google Search is enough for "what is X", "what happened with X", product issues, prices from stores,
  and anything a news site would carry. For what people are saying right now, use the X skill as well.
