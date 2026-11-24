---
name: whats-the-story
description: "What is the X issue / what happened with X / what are people saying about X": find out and report, without asking the user where to look.
---

The user heard of something and wants to know what it is: an outage, a controversy, a product issue, a
piece of tech gossip, a rumour. They have not said where to look, and do not expect to be asked.

Plan: one step, in Chrome, that reads the explanation and, when the thing is current (hours or days old,
or about what people are saying), a second step in X for the live reaction. Each step reports the facts
it read; finish with a short account: what it is, when it started, who is affected, what the sources say,
and what is still unclear.

- Step 1 (Chrome): a Google search for the thing by name (see the google-search skill: search by URL,
  `&tbm=nws` for news, `&tbs=qdr:d` when it is from today). Read the top two or three results, not the
  AI overview alone. `done_when`: "Noted what X is, when it started and what the sources say, from at
  least two result pages."
- Step 2 (X, only when it is current or the user asks what people are saying): the X search with
  `&f=live` (see the x skill). Read the first screen of posts and note the recurring claims, not every
  post. `done_when`: "Noted the main points people are making and any official account's post."
- Do not ask the user which source to use, and do not ask what X means: the search tells.
- Do not set up a step per source beyond these two: a reader on a third site is rarely worth a step.
- The final answer names its sources (the site or account) so the user can judge them.
