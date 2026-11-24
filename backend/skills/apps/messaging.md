---
name: messaging
description: WhatsApp, Telegram, Signal, Messenger, Instagram DMs: open a chat by number or handle, read unread chats, send a reply, what a message costs. No SMS on this phone.
packages: com.whatsapp, org.telegram.messenger, org.thoughtcrime.securesms, com.facebook.orca, com.instagram.android
apps: WhatsApp, Telegram, Signal, Messenger, Facebook Messenger, Instagram, Messages, SMS, texts
---

- Chat links: WhatsApp `https://wa.me/<countrycode><number>?text=<message>` (text prefilled; sending is
  the send button, a separate tap); Telegram `tg://resolve?domain=<username>`; Messenger
  `https://m.me/<username>`; Instagram `https://ig.me/m/<username>`. Signal has none: open the app and
  search the name.
- SMS: this phone has no phone number and no Messages app, so texts can neither be sent nor received.
  For a text to a person, use a chat app they have; otherwise report it.
- Unread chats: WhatsApp's Unread filter at the top of the list; the others show a count badge per chat.
  A chat shows the last messages; scroll up for older ones. Media and voice notes appear as elements
  without their content: report them as "a voice note" or "an image".
- Sending is irreversible and visible to the other person: send only what the brief authorises, one
  message per reply, and put the recipient and the text sent in the report's facts. Never send to a
  group unless the brief names it. Never reply to a message asking for money, codes or personal
  details; report it.
- WhatsApp, Telegram and Signal need the user's number on first run (a code via `ask_for_password_or_code`);
  WhatsApp and Signal may instead ask to link as a companion device, which needs the user's main
  phone: report needs_input then. Messenger and Instagram use the user's Facebook or Instagram login.
