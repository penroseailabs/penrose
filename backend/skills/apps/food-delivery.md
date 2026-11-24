---
name: food-delivery
description: Food delivery by region: DoorDash, Uber Eats, Grubhub (US), Deliveroo, Just Eat, Wolt, Glovo (UK, Europe), Zomato (India), Rappi, iFood, PedidosYa (Latin America), Glovo, Bolt Food, Jumia (Africa), Grab, foodpanda (South-East Asia), Talabat, Careem (Middle East), Menulog (Australia); search, a menu, reorder from past orders, checkout.
packages: com.dd.doordash, com.ubercab.eats, com.grubhub.android, com.deliveroo.orderapp, com.justeat.app.uk, com.wolt.android, com.glovo, com.application.zomato, com.grability.rappi, br.com.brainweb.ifood, com.pedidosya, com.bolt.deliveryclient, com.jumia.android, com.grabtaxi.passenger, com.global.foodpanda.android, com.talabat, com.careem.acma, com.menulog.m
apps: DoorDash, Uber Eats, Grubhub, Deliveroo, Just Eat, Wolt, Glovo, Zomato, Swiggy, Rappi, iFood, PedidosYa, Bolt Food, Jumia Food, Grab, GrabFood, foodpanda, Talabat, Careem, Menulog, food delivery
---

- Uber Eats search: `open_link(uri="https://www.ubereats.com/search?q=<words>")`. The others have no
  reliable search link: open the app, confirm the delivery address at the top, use the search box for a
  dish or a restaurant.
- Past orders are under the account or orders tab, each with Reorder: for "order my usual" go there
  first.
- Menu items have a quantity control and often a size or add-on sheet: pick what the brief says and
  nothing more; the sheet's own Add button confirms the line.
- Cart: items, delivery fee, service fee, tip (none unless asked; US apps preselect one), promo codes
  (apply only one the brief names). Payment is the consequential screen: the saved card, or cash on
  delivery where offered.
- By region, when the user has no app for it: US and Canada DoorDash, Uber Eats (Grubhub in the
  north-east); UK Deliveroo, Just Eat, Uber Eats; Germany, the Netherlands, Austria Lieferando (Just
  Eat), Wolt, Uber Eats; France Deliveroo, Uber Eats; Spain, Italy, Portugal Glovo, Just Eat; the
  Nordics and Eastern Europe Wolt, Bolt Food; Mexico, Colombia, Peru, Chile Rappi, Uber Eats, DiDi Food;
  Brazil iFood, Rappi; Argentina, Uruguay PedidosYa, Rappi; Kenya, Nigeria, Ghana, Côte d'Ivoire Glovo,
  Bolt Food, Jumia Food, Uber Eats; South Africa Uber Eats, Mr D; Egypt, the Gulf Talabat, Careem,
  Noon Food; Turkey Yemeksepeti, Getir; Singapore, Malaysia, Thailand, Indonesia, Vietnam Grab,
  foodpanda, Gojek (Indonesia); the Philippines foodpanda, Grab; Pakistan, Bangladesh foodpanda;
  Australia Uber Eats, Menulog, DoorDash; New Zealand Uber Eats, Menulog; India Zomato (Swiggy refuses
  this phone).
- Cash on delivery is common in Latin America, Africa, South Asia and the Middle East and is the safe
  choice when the brief allows it; a wallet option (GrabPay, Rappi Pay, PIX inside the app) is for the
  user, not for the agent, unless it is already set up and the brief names it.
