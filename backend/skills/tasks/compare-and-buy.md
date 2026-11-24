---
name: compare-and-buy
description: Find an item in the apps the user uses, compare, and order from the best one, with the order as its own verified step.
---

The user wants a thing bought or delivered ("get me milk", "order a phone charger") and the choice of app
or product is partly open.

- One step per app the user already uses for this (feedback memories and `installed_apps` say which;
  with none, the one or two best-known apps for the category). Each step: search, pick the matching
  item, read price, quantity and delivery time, and report them as facts. No cart changes yet.
- When the user named the app and the item exactly, skip the comparison: one step that builds the cart.
- Then one step that adds the chosen item to the cart in the chosen app, with the exact item and quantity
  in `instructions`, and `done_when` naming the cart's contents. Verify it with `verify_step` when the
  next step pays.
- Paying or placing the order is a step of its own, after the cart is verified, only with the user's
  clear authorisation in `context`, and with the payment method the user asked for (a memory may say
  which they prefer; it does not authorise paying).
- The final answer: what was ordered, the price, the delivery time and the order reference from the facts.
