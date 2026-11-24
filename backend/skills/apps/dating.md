---
name: dating
description: Tinder, Bumble and Hinge: matches and their chats vs the swipe deck, replying, what must never be done on the user's behalf.
packages: com.tinder, com.bumble.app, co.hinge.app
apps: Tinder, Bumble, Hinge, dating
---

- No usable deep links. The screen has two parts: the deck (profiles to like or pass) and Matches, where
  the chats are. A reply task lives in Matches; never like or pass in the deck unless the brief asks.
- Bumble: after a match a woman writes first and the match expires in 24 hours; "expiring" badges mark
  the ones that need a message now. Hinge: likes come with a comment on one photo or prompt; a chat
  starts from Matches. Tinder: Matches are at the top of the Messages tab; a match with no chat yet
  is a new match.
- A reply sounds like the user: the user's facts and tone from memories and the brief, nothing invented
  about them. Never give out the user's phone number, address, workplace, or agree to a time and place to
  meet; when a chat reaches that point, stop and report needs_input with the chat so far.
- These apps may refuse a rooted phone with a "something went wrong" or sign-in loop; that is final.
- Every message sent is listed in the report's facts with the match's name.
