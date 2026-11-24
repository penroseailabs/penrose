---
name: x
description: X (Twitter): searching posts by URL, Latest vs Top, reading a thread, what needs a login.
packages: com.twitter.android
apps: X, Twitter
---

- Search by URL: `open_link(uri="https://x.com/search?q=<words>&f=live")` opens the search in the X app
  when it is installed, else in Chrome. `f=live` is Latest (what people are saying now); drop it for Top.
  Encode spaces as `+`; `from:<handle>` limits to one account.
- Search, profiles and threads need a signed-in account. The user's session is on the phone when the app
  is installed and signed in; if a sign-in wall shows, log in as an ordinary app login (phone or email,
  then a code via `ask_for_password_or_code`). Never create an account.
- The feed moves while you read: note what matters as you go, and read the posts in front of you rather
  than scrolling for the one you saw a moment ago.
- A post is a numbered element; tapping it opens the thread with replies. Press_key back returns to the
  results. The "For you" and "Following" tabs are the home feed, not search.
- Tech news, product outages, "the X issue everyone is talking about", opinions and reactions: X Latest
  shows what is being said; a Google search gives the explanation. Use both for such a question.
