---
name: learn-from-videos
description: "How do I fix / build / cook / do X", "is X worth it / which X should I buy", "explain X in depth", "tips for X", a YouTube link: research that lives in YouTube videos and their comments; read them instead of watching.
---

The user wants to learn something or decide something, and the best material is people explaining it
on video: a repair or DIY guide, a product review or comparison, a deep dive, a talk, a lecture, a
topic the YouTube creators cover well (cooking technique, a game's mechanics, a language, a gadget, a
car, software, finance explainers). They did not say "watch" or "YouTube" and do not want to be asked.

Where videos are the best source (read them before a Google step, alongside one when written facts matter):
- Repair and troubleshooting: an appliance's error code, a car noise, a leak, a phone or PC fault ("dishwasher
  E15", "brake squeal when cold"); the comments often hold the fix that worked for most people.
- Home, DIY and making: home improvement, gardening, sewing and crafts, 3D printing, woodworking.
- Cooking: a recipe the user will cook, a technique (knife skills, sourdough, smoking), what goes wrong;
  give the quantities and steps, and the comments' tweaks.
- Buying: reviews and comparisons, long-term reviews ("after one year"), what to check when buying used,
  setup and first steps after buying, teardowns and repairability.
- Software and gadgets: how to do something in an app (Excel, Lightroom, a phone setting), setting up a
  device, a game's walkthrough or build.
- Skills: exercise form and routines, an instrument, a language, drawing, a sport's technique.
- Places: travel tips and itineraries, what to avoid, a neighbourhood or hotel walk-through, a venue, a
  city's food spots.
- Ideas: explainers and deep dives, lectures, talks and podcasts, commentary on a news story (as
  opinion, beside written news).
- A YouTube link the user sent ("what does this say", "is this right"): read it with comments, no search.

Plan: no phone step. You do it yourself with `youtube`, which reads captions on the computer in a
few seconds.

1. `youtube` with `search`, worded as one would search YouTube (see the youtube skill): "how to fix a
   running toilet fill valve", "dyson v15 vs shark review", "how do transformers work explained".
2. Read the list and pick two or three videos (right length, a channel that is about the subject,
   different creators). Call `youtube` with `videos`, with `comments: true` for guides and reviews:
   viewers add the fixes for what went wrong, corrections and how a product held up. For a long one, read
   the chapters and call again with `start`/`end` for the part that answers the question.
3. Answer from what they say: for a guide, the steps in order with the tools, parts and sizes
   named, and what the videos warn goes wrong; for a review, what each reviewer found, where they
   agree and disagree, and who it is for; for a deep dive, the explanation in plain words with the key
   points and any numbers. Say so where the videos disagree, and add what many viewers say in the
   comments (a common pitfall, a better part, a failure after months) as "viewers say". Name each video
   (title, channel) with its link, with `&t=<seconds>s` for the part you used.

- Combine with `look_up` when the user's own thing matters: the model they own (the order email),
  what they bought before, or a price or stock the videos cannot tell. Look that up first, then
  search for that exact model.
- A guide for something dangerous or regulated (mains electrics, gas, brakes, medication): give the
  steps the videos agree on, and say plainly where a qualified person is needed.
- Reviews are often sponsored or given free: say when a video you rely on is, and prefer a measured
  result to an adjective. If every reviewer is sponsored by the maker, say that.
- Automatic captions garble names and model numbers: confirm any number or part name that matters
  against another video or a quick Google search before the user buys or cuts anything.
- If every video fails to read (no captions, YouTube refusing), say so and fall back to the way you
  would have done it: a Google search in Chrome (the google-search skill), or a phone step in the
  YouTube app that opens a video's description and "Show transcript".
- Do not run a phone step just to "watch" a video: the screen shows the picture, never the words.
