---
name: ride-hailing
description: Uber, Lyft, Bolt, Grab, Gojek, inDrive, Yango, Careem, Cabify, DiDi, 99, Ola and Rapido by region: a ride to an address by deep link, the fare before confirming, what the phone's location must be.
packages: com.ubercab, me.lyft.android, ee.mtakso.client, com.grabtaxi.passenger, com.gojek.app, sinet.startup.inDriver, com.yandex.yango, com.careem.acma, com.cabify.rider, com.didiglobal.passenger, com.taxis99, com.olacabs.customer, com.rapido.passenger
apps: Uber, Lyft, Bolt, Grab, Gojek, inDrive, Yango, Careem, Cabify, DiDi, 99, Ola, Rapido, cab, taxi, ride
---

- Uber: `open_link(uri="https://m.uber.com/ul/?action=setPickup&pickup=my_location&dropoff[formatted_address]=<address>")`
  opens the app with the drop set; the ride options and fares show before anything is booked. Lyft,
  Bolt, Grab, Ola and Rapido: open the app and type the drop in the "Where to" box.
- Pickup is the phone's location, which is the computer's (`phone_location` in the brief). If the user
  says they are elsewhere, the brief gives the pickup address: set it in the app's pickup field; the
  phone's location itself is never changed.
- By region, when the user has no app for it: US and Canada Uber, Lyft; UK and most of Europe Uber,
  Bolt, FreeNow; Spain Cabify, Uber, Bolt; Russia and Central Asia Yango; Mexico Uber, DiDi, Cabify;
  Brazil Uber, 99; Argentina, Chile, Colombia, Peru Uber, Cabify, DiDi, inDrive; Ghana, Nigeria, Kenya,
  South Africa Bolt, Uber, Yango, inDrive; Egypt and the Gulf Careem, Uber; Indonesia Gojek, Grab;
  Singapore, Malaysia, Thailand, Vietnam, the Philippines Grab; India Uber, Ola, Rapido (bike and auto);
  Australia Uber, DiDi.
- A fare estimate without an account: most apps show nothing before sign-in. When the user does not
  want to log in, the honest answer is an estimate from the web (the app's own fare estimator page, a
  search for "<app> fare <from> to <to>"), given as a range with its source and date.
- Each option (economy, XL, comfort, bike, auto) shows a fare and a wait time; the brief's choice, else
  the cheapest fitting one. "Confirm" or "Book" is the consequential tap, after which a driver is
  assigned and cancelling may cost a fee: never book twice.
- After booking, the driver's name, vehicle and ETA are on the screen and go in the facts.
- Payment: the card saved in the app; cash where the app offers it (India, South-East Asia).
