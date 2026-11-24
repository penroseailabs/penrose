# Contributing to penrose

Thank you for wanting to help. This page says how to report a problem, how to get the code running, and what a
change needs before it can be merged.

## Reporting a problem or asking for something

- **A bug:** open an issue with the bug form. Say what you asked penrose to do, what it did, and what you
  expected. The build number is in the penrose menu under About.
- **An idea:** open an issue with the idea form. Describe the situation you were in, not only the feature.
- **A security problem:** do not open an issue. See [SECURITY.md](SECURITY.md).

Before you attach a log or a screenshot, check it for anything private. penrose's logs and screenshots can show
your messages, your orders and your address.

## Getting the code running

[docs/DEVELOPING.md](docs/DEVELOPING.md) explains how the parts fit together, and
[docs/DESKTOP.md](docs/DESKTOP.md) covers the desktop app, the phone's set-up and releases. In short:

```bash
python -m venv .venv
.venv/Scripts/pip install -r backend/requirements-dev.txt    # macOS and Linux: .venv/bin/pip
npm --prefix frontend ci
npm --prefix desktop ci
npm --prefix desktop run dev
```

The first run downloads and builds the phone, which takes a few minutes. The phone-side code (the Xposed module,
the home app and the Magisk module) is in `android/`; see [android/README.md](android/README.md). The built phone
apps in `desktop/assets/android` are signed with the project's release key, which is not in the repository. A
change to `android/` is built and signed by a maintainer before a release.

## Before you open a pull request

Run the checks that CI runs:

```bash
cd backend && python -m pytest -q
cd desktop && npm test
cd frontend && npm run typecheck
cd coordinator && npm test
```

A good pull request:

- does one thing, and says what and why in its description;
- has a test for a fix or a new behaviour, where one is possible without a phone;
- keeps the safety rules intact. penrose's limits (no deleting email, no money apps, passwords and one-time
  codes kept from the model, asking before a purchase) live in code on purpose. A change that loosens one needs
  an issue and a maintainer's agreement first;
- uses the product's name as it is written: penrose, always lowercase.

## License

penrose is released under the [GNU AGPL 3.0](LICENSE), with a commercial license on offer for uses the AGPL does
not fit ([LICENSING.md](LICENSING.md)). So that both can be offered, a pull request is accepted once you have
agreed to the [contributor license agreement](CLA.md), by ticking its box in the pull request form. You keep the
copyright in what you write.
