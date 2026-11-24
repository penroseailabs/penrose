---
name: dating-replies
description: Reply to matches on a dating app in the user's voice, keep conversations going, and stop for the user at anything personal: numbers, plans to meet, anything they would want to say themselves.
---

Same shape as reply-to-messages, with the stakes of talking as the user to someone they may meet.

- One step per app (Tinder, Bumble, Hinge). The step: open Matches, read each chat with a new message
  (and, on Bumble, each expiring match), reply where the rules allow, report every chat: match's name,
  what they said, what was sent or why it waits.
- `context` carries the user's voice and facts (from `user` memories and the request: what they do,
  where they live roughly, interests, how they joke) and the lines: never the phone number, address,
  workplace or exact plans; never a time and place to meet; never a claim about the user that is not in
  the facts; never an answer to "are you a bot". When a chat reaches any of these, the step reports it
  for the user with the chat so far.
- New matches with no message yet get an opener only if the request asks for openers; it refers to
  something in their profile, which the subagent reads first.
- No swiping, liking or passing in the deck unless the request asks, with a number.
- The final answer: who got a reply and what was said, and who is waiting for the user, with the chat.
