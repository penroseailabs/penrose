---
name: email-search
description: Searching the user's email well: Gmail's operators in a look_up query, the kinds of email each question needs (order confirmations, delivery updates, refunds, card alerts, statements, bills, receipts, invoices, subscriptions, bookings) and the noise to leave out (cart reminders, browsing-triggered offers, codes).
tools: look_up
---

Gmail answers at most 25 emails per query, newest first. A plain word such as "amazon" fills them with
whatever has the word: invoices, sign-in codes, job alerts, card statements with the
store's name, and several emails for every parcel. Write a query that asks for the kind of email the question
needs instead: a `look_up` query with Gmail's operators is searched in Gmail only (not on Google, the phone,
the user's browsing, notifications or earlier chats), so give those a plain-word query of their own when they matter.

Gmail's syntax (no regular expressions, no wildcards):
- Words must all match, anywhere in the email; `"exact phrase"`; `OR` in capitals; `( )` groups; `-` leaves
  out (`-subject:shipped`); `{shipped delivered}` is any of them.
- `from:amazon` matches the sender's name or address (Amazon.com, amazon.co.uk, Amazon Pay, AWS alike, so add
  subject words); `subject:(ordered OR "your order")`; `has:attachment`, `filename:pdf`; `newer_than:30d`
  (d, m, y), `after:2025/01/01 before:2025/02/01`; `category:promotions` (or primary, updates, social);
  `is:unread`, `is:starred`.
- Gmail matches a few forms of a word, not all: list the ones subjects use (shipped, shipping; canceled,
  cancelled).
- 25 emails with the oldest still recent: there are more. Ask again with `before:` the oldest one's date to
  go further back, or narrow by store or time.

Orders, from any store (one order sends placed, shipped, out for delivery, delivered, a review request,
sometimes an invoice; then cancelled, return, refund). Ask for the stage the question is about:
- What was bought, an order history, the usual order, how often, what it cost: the confirmations, one per
  order. `from:<store> subject:(order OR ordered OR "order confirmation" OR "order placed" OR "thanks for
  your order" OR receipt) -subject:(shipped OR delivered OR "out for delivery" OR cancelled OR refund)`.
- Where is it, has it come: the item's word with the shipping stages, the newest is where it stands.
  `<item> subject:(shipped OR delivered OR "out for delivery" OR arriving OR delayed)`. Delivered, or a
  review request (`"rate your" OR "how was"`), means it arrived.
- Returns, refunds, cancellations: `<store or item> subject:(refund OR refunded OR return OR returned OR
  cancelled OR canceled)`; the newest says where it stands.
- Any store at all (which shops does the user use): the confirmation subjects without `from:`, plus
  `-category:promotions`; brokerage confirmations ("buy order") and warranty mails come too, so read the senders.
- Food and grocery delivery send one email per order, often "delivered": `from:doordash subject:(order OR
  receipt OR delivered)` with a date range answers "how much on DoorDash last month".

Money:
- Card and bank alerts, a charge from a merchant: `<merchant or bank> subject:(transaction OR spent OR
  debited OR charged OR purchase OR "you made")` with `newer_than:` or a date range.
- Statements: `from:<bank> subject:statement has:attachment`, a month by date range.
- Bills due: `subject:(bill OR "amount due" OR "payment due" OR "due date" OR "is ready")`.
- Receipts and payments made: `subject:(receipt OR "payment received" OR "payment confirmation" OR "thank you
  for your payment" OR "payment successful")`; invoices: `{subject:invoice filename:invoice}`.
- Subscriptions: what the user pays for is in the app stores' receipts (`from:(apple OR "google play")
  subject:receipt`) and in charges that repeat month after month; started `subject:(welcome OR "subscription
  confirmed" OR trial OR membership)`; renewing `subject:(renewal OR renews OR "will renew" OR "auto-renew")`;
  ended `subject:(cancelled OR canceled OR expired OR ended) subscription`.

Bookings: `subject:(itinerary OR "e-ticket" OR "boarding pass" OR "booking confirmation" OR "booking
confirmed" OR reservation OR "your trip") -category:promotions`, with the airline, hotel or city as a word. The
date of travel is in the email, not its date: read which ones are still ahead.

Noise, left out unless the task is about it:
- Offers: cart reminders ("left something", "still in your cart"), browsing-triggered mail ("still
  interested", "you viewed", "inspired by", "price drop", "back in stock"), sales and newsletters. Most sit in
  Promotions: `-category:promotions`. A few real receipts land there too (older ones, small stores), so drop
  it before concluding an order does not exist.
- Codes and account mail: `-subject:(code OR OTP OR verification OR login OR "sign in" OR password)`.
- The store's name in mail that is not from it (job alerts, co-branded cards, news): `from:<store>`, not the
  bare word.
- Offers are the point when the task is about what the user wants (a gift for them, "the one I was looking
  at"): `category:promotions (cart OR "left something" OR "still interested" OR "you viewed" OR "price drop"
  OR "back in stock")` shows what they looked at and left; their browsing (a plain query) shows more.
