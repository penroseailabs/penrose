# Security

penrose is trusted with a phone that is signed in to your accounts, so we treat security reports as urgent.

## Reporting a vulnerability

Please report it privately, not in a public issue:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**.
3. Describe the problem, how to reproduce it, and what an attacker could do with it.

We aim to reply within a few working days. We will tell you what we found and when a fix will ship, and we
will credit you in the release notes unless you would rather we did not.

## What counts

Anything that lets someone other than the user read or act on the user's phone, accounts, keys or conversations.
For example:

- another program or web page on the same computer reaching penrose's local services without its token;
- text on the phone's screen, in a notification or in a message making penrose act for someone other than the
  user (prompt injection) in a way that gets past its limits;
- a password, a one-time code or an API key reaching an AI model, a log or the chat;
- the link between the penrose phone app and the computer being read or changed by anyone in between;
- an update or a phone-side component being replaced by one that the project did not build.

## Supported versions

Only the latest release is supported. penrose updates itself, so the fix for a reported problem ships as a new
release.
