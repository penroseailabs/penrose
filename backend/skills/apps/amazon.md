---
name: amazon
description: Amazon in any country by deep link: search, cart and orders on the user's Amazon domain; Buy Now vs cart; what to avoid at checkout.
packages: com.amazon.mShop.android.shopping, in.amazon.mShop.android.shopping
apps: Amazon, amazon.com, amazon.co.uk, amazon.de, amazon.in, amazon.ca, amazon.com.au
---

- The domain is the user's country's: amazon.com (US), .co.uk, .de, .fr, .ca, .com.au, .co.jp, .in,
  .ae; from a user memory, else `locale`. The app shows its country in the top bar.
- Search: `open_link(uri="https://www.amazon.<tld>/s?k=<words>")` (spaces as `+`). Cart:
  `https://www.amazon.<tld>/gp/cart/view.html`. Orders: `https://www.amazon.<tld>/gp/your-account/order-history`.
  If a link opens in Chrome rather than the app, the web page works the same; stay there.
- Results list "Sponsored" items first: skip them unless they are the exact item. Read price, rating,
  delivery date and "Sold by" from the list; open an item only to confirm size, variant or seller.
- "Buy Now" skips the cart and goes straight to checkout: use "Add to Cart" so the cart can be verified.
  Checkout pages: address, then payment, then "Place your order" (the consequential tap).
- Traps: "Subscribe & Save" is preselected on some items (choose one-time purchase); quantity defaults
  to 1; Prime trial, "Pay Later" and credit-card offers are declined; a size or colour variant must be
  chosen before the price shown is the price paid.
- Payment: the card saved in the account, or cash on delivery where the country offers it.
