---
name: flipkart
description: Flipkart (India) by deep link: search, cart, orders; variants and sellers; checkout order of screens.
packages: com.flipkart.android
apps: Flipkart
---

- Search: `open_link(uri="https://www.flipkart.com/search?q=<words>")`. Orders: `https://www.flipkart.com/account/orders`.
  The app may show a login (phone number, then OTP via `ask_for_password_or_code`) before the first search.
- Results: price, rating and an "Assured" badge are in the list; "Sponsored" items are not the answer.
  Variants (size, colour, storage) are chosen on the item page before Add to cart.
- Checkout: cart, then address, then order summary, then payment, then "Confirm Order" (the consequential
  tap). "Buy Now" skips the cart: use Add to cart so the cart can be verified.
- Exchange offers, protection plans and "Pay later" are declined unless the brief asks for them.
