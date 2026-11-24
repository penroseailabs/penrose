---
name: linkedin
description: LinkedIn jobs by deep link: search with filters, Easy Apply's pages, the resume picker, messaging.
packages: com.linkedin.android
apps: LinkedIn
---

- Job search: `open_link(uri="https://www.linkedin.com/jobs/search/?keywords=<words>&location=<city>")`.
  Add `&f_AL=true` for Easy Apply jobs only, `&f_TPR=r86400` for the last day (`r604800` a week),
  `&f_WT=2` for remote. The app opens these when installed and signed in; else Chrome does, signed out.
- A job page shows title, company, location, posted age and the Easy Apply or Apply button. "Apply"
  (without Easy) leaves for the company's site: a different form each time, often a separate account.
- Easy Apply is a sheet of 2 to 5 pages: contact details (prefilled), resume (the last uploaded one is
  preselected; "Upload resume" opens the phone's file picker, where Google Drive and Downloads are the
  sources), screening questions (years of experience, work authorisation, start date or notice period, salary), then
  Review and the "Submit application" button, which is the consequential tap.
- Answer screening questions from the brief and the user's facts only; a question the brief does not
  settle ends the step with needs_input, listing the question and the answers offered.
- Messages: `https://www.linkedin.com/messaging/`. Recruiter messages sit there and in notifications.
