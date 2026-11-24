---
name: groceries
description: Grocery delivery by region: Instacart and Walmart (US), Tesco, Ocado, Getir (UK, Europe), Woolworths, Coles (Australia), Rappi, Jumia (Latin America, Africa), Blinkit and Zepto (India); search by link, the delivery address, pack sizes and quantities, checkout.
packages: com.instacart.client, com.walmart.android, com.tesco.grocery.view, com.ocado.mobile.android, com.getir, au.com.woolworths, com.coles.android.shopmate, com.grability.rappi, com.jumia.android, com.grofers.customerapp, com.zeptoconsumerapp
apps: Instacart, Walmart, Tesco, Ocado, Getir, Woolworths, Coles, Rappi, Jumia, Blinkit, Zepto, Instamart, groceries
---

- Search links: Instacart `https://www.instacart.com/store/s?k=<words>`; Walmart
  `https://www.walmart.com/search?q=<words>`; Blinkit `https://blinkit.com/s/?q=<words>`; Zepto
  `https://www.zeptonow.com/search?query=<words>`. Getir: the search box on its home screen. If a link
  lands on the app's home instead, the search box is at the top.
- Every one needs a delivery address (and Instacart a store) before anything shows a price. Grant
  location, or pick the saved address the brief names; "not available in your area" or "store closed"
  is a fact to report, not a retry.
- Items show pack size next to the name: match the size the brief asks for, and set the quantity with
  the + control on the item, not by adding it twice. Instacart's "replacement" choice per item: pick
  "don't replace" unless the brief says otherwise.
- The cart shows item lines, delivery fee, service fee and tip (set tip to none unless asked). Payment
  is the last screen: the saved card, or cash on delivery where offered (India).
- By region, when the user has no app for it: US Instacart, Walmart, Amazon Fresh; Canada Instacart;
  UK Tesco, Sainsbury's, Ocado, Getir; Germany, the Netherlands, France Picnic, Flink, Getir, the
  supermarket's own app (Rewe, Albert Heijn, Carrefour); Spain, Italy Glovo, the supermarket's app
  (Mercadona, Esselunga); Australia Woolworths, Coles; Mexico, Colombia, Brazil Rappi, Cornershop by Uber,
  the supermarket's app; Kenya, Nigeria, Ghana Jumia, Glovo; the Gulf Talabat Mart, Careem Quik, Noon
  Minutes; Singapore, Malaysia, Thailand, Indonesia GrabMart, foodpanda shops; India Blinkit, Zepto
  (Swiggy Instamart refuses this phone). Supermarket apps usually want a delivery slot picked before checkout; delivery minimums and
  slot fees go in the report.
