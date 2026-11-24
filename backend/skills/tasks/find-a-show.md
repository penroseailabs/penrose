---
name: find-a-show
description: What is on (films, showtimes, streaming, concerts, events) is a question, answered by search; booking a ticket is an action in a ticket app, as its own step.
---

"What is playing this weekend", "where can I watch X", "any good concerts next month" are questions.
A question is answered where the answer is published: one Chrome step, a Google search by URL (the
google-search skill), with the user's area from `locale` or the memories in the query when the answer
depends on it. Google's own panels carry what is on at cinemas, where a title streams and at what
price, and what events are near; the search page is read, not the apps behind it. Report titles,
times, places, prices and ratings as found; a recommendation picks three from that list with a line
each.

A ticket app is for acting: booking a seat for a show the user has chosen (or the request fixed), with
the time, the seat count and the payment limits in `instructions`, as its own step after the question
is answered, and verified before paying. The tickets skill names the apps by region.
