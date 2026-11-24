---
name: payments
description: Paying on this phone: wallet and transfer apps that check device integrity (Google Pay and Wallet; PhonePe and Paytm in India; PayPal, Venmo and Cash App may) refuse a rooted device, so pay inside the app with a saved card, or pay on delivery.
packages: com.google.android.apps.walletnfcrel, com.google.android.apps.nbu.paisa.user, com.phonepe.app, net.one97.paytm, com.paypal.android.p2pmobile, com.venmo, com.squareup.cash
apps: Google Pay, Google Wallet, GPay, PhonePe, Paytm, PayPal, Venmo, Cash App, UPI
---

- Google Pay and Google Wallet (and PhonePe and Paytm in India) check the device and refuse a rooted phone; PayPal,
  Venmo and Cash App may. A "device not secure", "rooted", "something went wrong" or blank screen in
  them is final: do not reinstall, retry or look for a workaround.
- So a payment inside a shopping or booking app uses, in this order: a card already saved in that app;
  cash on delivery when offered and the brief allows it; a card whose details the user gives through
  `ask_for_password_or_code` (never typed from memory, never guessed). A PayPal button inside an app opens PayPal's
  web login: that works with the user's login via `ask_for_password_or_code`. "Pay with Google Pay", UPI (India) and
  "open wallet app" options will fail: do not choose them.
- Before the confirming tap, check the amount and currency against the brief and the cart; put the
  order reference from the confirmation screen in the report's facts. A failed payment is reported,
  never retried blindly.
