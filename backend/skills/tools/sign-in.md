---
name: sign-in
description: How the big identity providers sign people in (Microsoft Entra, Google, Apple, Meta, OpenAI, Anthropic), how to recognise each screen, what to do and what to tell the user in one sentence, when to wait, and the loop to avoid once a second factor is pending.
tools: ask_login, ask_for_password_or_code, ask_user
---

A sign-in is a conversation between the site, the user's other devices and the user. The agent's part is
small: type what it may type, read the screen exactly, tell the user what the screen is waiting for, then
wait. Most failed sign-ins are not refused credentials; they are the agent acting while the site was
waiting for the user.

## The rule for second factors

Once the screen shows a second-factor prompt of any kind (a number to match, "approve on your phone",
"tap Yes", a code sent to a device the agent does not have, a passkey or screen-lock request for another
device), the agent stops acting:

- Report the exact prompt: the number or code shown, the device or app it names, and where the user must
  look ("the Microsoft Authenticator app", "the Gmail app on your phone", "your iPhone").
- Do not go Back, do not reload the page, do not return to the sign-in page, do not re-enter the email or
  password, do not tap "Try another way" or "Use a different account", and do not press Send again while
  that prompt stands. Any of those cancels the prompt the user is answering and starts a new one with a
  new number or code.
- Wait for the user's word. The subagent calls `ask_login` with the exact prompt in `why` (the number
  included); the orchestrator passes it on word for word. Checking the screen while waiting is fine; acting
  on it is not.
- Only once the user says they approved (or the prompt times out and the screen says so) does the agent
  continue from the screen. A timed-out prompt may be resent once; a prompt the user says they approved
  that still has not moved is checked again after a few seconds, not resent.

A code is asked for only when the screen has a field for it. A screen that shows a number but no field is
telling the user what to approve elsewhere, never asking the agent to type that number.

## What to tell the user

One sentence the voice desk can say as it is, naming the app, the device and the exact value:

- "Microsoft shows the number 82: open the Authenticator app on your phone and tap the 82 to approve."
- "Google sent a prompt to your phone: open it and tap Yes, it's me."
- "Apple sent a six-digit code to your trusted devices: tell me the code."
- "Instagram sent a login code to the email ending in …@gmail.com: tell me the code."
- "ChatGPT emailed a sign-in link to you@example.com: tell me the code in it, or open the link on your phone."
- "The site wants a password for you@company.com: tell me when you've entered it on the phone panel, or give it to me privately."

Say what the screen says, not what it probably means. A wrong device name sends the user looking in the
wrong place.

## Microsoft Entra ID and Microsoft accounts

Used by company portals (benefits, HR, payroll, Teams, Outlook, SharePoint), by "Sign in with Microsoft"
on other sites, and by Microsoft's own consumer services. The pages are `login.microsoftonline.com` or
`login.live.com`, sometimes a company-branded page with the same shape. Many portals offer both their own
"manual login" and "Sign in with Microsoft / SSO": pick one and stay with it; the one the user's company
uses is SSO unless the user says otherwise.

| Screen | Recognise it by | Do |
|---|---|---|
| Pick an account | "Pick an account", a list of emails, "Use another account" | Tap the account the task is for; "Use another account" only if it is not listed. |
| Email | "Sign in", one field "Email, phone, or Skype", Next | Type the email from the brief or saved sign-ins. Next. |
| Password | "Enter password", the email shown above the field, "Forgot password?" | Saved password if offered, else `ask_for_password_or_code` for the password. Sign in. A wrong-password error allows one more try with the user's help, not a third. |
| Approve sign in request (number matching) | "Approve sign in request", "Open your Authenticator app, and enter the number shown to sign in", a large two-digit number, "I can't use my Microsoft Authenticator app right now" | Second factor pending: STOP. Tell the user the number: "Microsoft shows the number NN: open Authenticator on your phone and tap NN." Wait. The number is not typed anywhere by the agent. The screen moves on by itself when the user approves. |
| Approve sign in request (no number) | Same page without a number, "We've sent a notification to your mobile device" | Second factor pending: STOP. "Microsoft sent an approval to the Authenticator app on your phone: tap Approve." Wait. |
| Enter code | "Enter code", a field, "Enter the code displayed in the Microsoft Authenticator app" or "We texted your phone +X XXX-XXX-XX12" | A code is really wanted: focus the field, `ask_for_password_or_code` for it ("the 6-digit code from the Authenticator app for Microsoft", or "the code Microsoft texted to the number ending 12"). |
| Verify your identity | "Verify your identity", a list: "Approve a request on my Microsoft Authenticator app", "Use a verification code", "Text +X …", "Call +X …" | Choose the method the user prefers; by default Authenticator approval. Then follow the row above for that method. Do not switch methods while one is pending. |
| Stay signed in? | "Stay signed in?", "Don't show this again", Yes / No | Tap Yes (the phone is the user's). Not a second factor. |
| More information required | "More information required", "Your organization needs more information to keep your account secure", Next | MFA enrolment (Authenticator setup with a QR code). The agent cannot do this: `ask_login` saying "Microsoft wants to set up the Authenticator app for this account; please do it on the phone panel or on your own device." |
| Conditional access / blocked | "You cannot access this right now", "Your sign-in was successful but does not meet the criteria", "device must be managed/compliant", "Action required: register your device" | Not solvable from this phone. Report it to the user as the exact message; do not retry or try the manual login as a way round it. |
| Help us protect your account / Update your password | A forced password change or security info review | `ask_login`; the user decides. |
| Temporary password email | A portal's own "we emailed you a temporary password" | That is the portal's manual login, not Microsoft's: use it only if the user chose the manual login. If SSO is in progress, do not mix the two. |

The loop to avoid, on a company portal that signs in with Microsoft: the number-matching page was shown,
the agent treated it as a code request and asked the user for a six-digit code, then went back to the SSO
page, re-entered the password, got a new number, and repeated. Each pass cancelled the approval the user
could have tapped. The right pass: password → "Approve sign in request" with the number 82 → tell the
user "approve the tap for number 82 in Authenticator" → wait → the portal opens.

## Google

Pages are `accounts.google.com`; also the "Choose an account" sheet inside apps using the phone's own
Google account. On penrose's phone the user's Google account is signed in already: "Continue with Google"
in an app or site usually only needs the account tapped in the chooser, with no password.

| Screen | Recognise it by | Do |
|---|---|---|
| Choose an account | "Choose an account", the phone's accounts listed, "Use another account" | Tap the task's account. "Use another account" only if it is not listed; never add an account to the phone. |
| Email | "Sign in", "Email or phone", Next | Type the email. Next. |
| Password | "Welcome", the email shown, "Enter your password", Show password | Saved password if offered, else `ask_for_password_or_code`. Next. |
| Check your phone / Tap Yes | "2-Step Verification", "Check your phone", "Google sent a notification to your <device>. Tap Yes on the prompt to sign in", sometimes "tap the number NN" | Second factor pending: STOP. "Google sent a prompt to your <device name>: open it and tap Yes" (and the number, when one is shown). Wait. "Try another way" only if the user asks for it. |
| Verify it's you with a passkey / screen lock | "Use your passkey", "Enter your screen lock for the selected device", "Verify it's you", a device list | The user's screen lock or another device: `ask_login` with the exact prompt. If the phone's own screen lock is asked for, the phone confirms it itself; never type one. |
| 2-Step code | "Enter the code", "Get a verification code from the Google Authenticator app" or "Google sent a text message to ·····12" | A code is wanted: `ask_for_password_or_code` naming the source. |
| Backup codes / Security key | "Enter one of your 8-digit backup codes", "Use your security key" | `ask_login`. |
| Confirm recovery details / "Verify it's you" with a recovery email or phone | A field asking for the full recovery phone or email | Only a value from the brief or the user; else `ask_login`. |
| Consent screen | "<app> wants to access your Google Account", Allow / Cancel, scopes listed | Allow is a decision for the task: allow when the task is to sign in with Google here; cancel when the scopes go beyond sign-in and the task did not ask for them, and tell the user. |
| Sync / "Turn on sync?" in Chrome | Chrome's own prompt after a Google sign-in | Not part of the sign-in: "No thanks" or the plain continue; it is handled by the phone setup on the user's own phone. |

## Apple ID (Sign in with Apple, iCloud, appleid.apple.com)

| Screen | Recognise it by | Do |
|---|---|---|
| Apple ID | "Sign in with Apple ID", email field | Type the email; Continue. |
| Password | Password field under the email | Saved if offered, else `ask_for_password_or_code`. |
| Two-Factor Authentication | "Two-Factor Authentication", "A message with a verification code has been sent to your devices. Enter the code to continue", six boxes | A code is wanted, sent to the user's trusted iPhone/iPad/Mac, not to this phone: "Apple sent a six-digit code to your Apple devices: tell me the code." `ask_for_password_or_code` with the fields focused. Do not tap "Didn't get a code?" unless the user says the code did not arrive. |
| Code by SMS | "Didn't get a code?" → "Text me", a phone number ending in … | Only on the user's word; then `ask_for_password_or_code`. |
| Trust this browser? | "Trust this browser?", Trust / Not now | Trust (the phone is the user's). Not a second factor. |
| Hide my email / Share my email | The Sign-in-with-Apple sheet | Share the real email unless the user said to hide it; it decides which account the site creates. |

## Meta: Facebook, Instagram, Messenger, WhatsApp

| Screen | Recognise it by | Do |
|---|---|---|
| Log in | Phone/email/username and password | Saved if offered, else `ask_for_password_or_code`. |
| Check your notifications / Approve from another device | "Check your notifications on another device", "Approve login from your other device", "We sent a notification to your <device>" | Second factor pending: STOP. "Instagram asks you to approve this login on your other phone: open the notification and tap It was me / Approve." Wait. "Try another way" only on the user's word. |
| Enter login code / confirmation code | "Enter the login code", "We sent a code to …", six boxes; or "Enter confirmation code" from an authenticator | A code is wanted: `ask_for_password_or_code` naming where it went (SMS to ·····12, email to ···@gmail.com, the authentication app, WhatsApp). |
| Was this you? / Suspicious login | "We noticed an unusual login", "This was me" / "This wasn't me" | "This was me" when the task is the user's sign-in; the user is told. |
| Save your login info? | "Save your login info?", Save / Not now | Save (the phone is the user's). |
| Account Center / "Continue as <name>" | A one-tap continue with a name and picture | Tap it when it is the task's account. |
| Login via WhatsApp | A code sent to WhatsApp on the user's own phone | `ask_for_password_or_code` naming WhatsApp; the agent does not read the user's other phone. |

## OpenAI / ChatGPT

Pages are `auth.openai.com` or `chatgpt.com`. Offers "Continue with Google", "Continue with Microsoft
Account", "Continue with Apple", or email.

| Screen | Recognise it by | Do |
|---|---|---|
| Welcome back / Create account | Email field with the "Continue with …" buttons under it | Use the method the user's account was made with; when unknown and the phone's Google account is the same address, "Continue with Google" (see Google above). Otherwise type the email; Continue. |
| Enter your password | Password field after the email | Saved if offered, else `ask_for_password_or_code`. |
| Check your inbox / Enter the code | "Check your inbox", "Enter the 6-digit code we sent to you@…" | A code is wanted, in the user's email: if the Gmail app on this phone holds that inbox, read the newest OpenAI mail there and type the code; otherwise "OpenAI emailed a code to you@…: tell me the code." |
| Verify you are human | A Cloudflare turnstile or puzzle | `ask_login` ("there is a human check"); never try to solve it. |
| Two-factor code | "Enter the code from your authenticator app" | `ask_for_password_or_code`. |

## Anthropic / Claude

Pages are `claude.ai/login`. Offers "Continue with Google" or email.

| Screen | Recognise it by | Do |
|---|---|---|
| Log in / Sign up | Email field, "Continue with Google" | Same choice as for OpenAI. With email, Claude sends a magic link and a code in the same mail. |
| Check your email | "Check your email", "We sent a login link to you@…", a code field | Read the newest Anthropic mail in the Gmail app if that inbox is on this phone, and type the code; otherwise "Claude emailed a sign-in code to you@…: tell me the code." The link in the mail opens the session in the browser that opened it; the code works in the field that is already open, so prefer the code. |

Do not install the Claude Android app: on penrose's phone it rejects sign-in (it fails
the Play Integrity check, which the phone does not pass). Use Claude as a Chrome
web app instead: open `https://claude.ai` in Chrome and sign in there as above.

## The general shape, for any other provider

1. Choose the method the user's account was made with. Two or more methods started at once (SSO and the
   site's own password, two codes sent) make the later one win and the earlier one fail.
2. Email, then password: saved first, else asked privately, at most two tries.
3. The second factor: read the screen for one of three shapes. (a) A number or an "approve" with no
   field: tell the user the number and the device, wait. (b) A field for a code: ask for the code,
   naming where it was sent. (c) Something only the user can do (a device choice, a passkey on another
   device, a captcha, enrolment): `ask_login` with the exact prompt.
4. Post-login prompts ("Stay signed in?", "Trust this browser?", "Save password?", "Turn on
   notifications?"): say yes to staying signed in and no to anything that is not the task.
5. After a wait of more than a couple of minutes with no change, ask the user whether the prompt reached
   them before resending; resend once at most; then `ask_login`.
