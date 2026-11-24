---
name: apply-to-jobs
description: "Apply to jobs that fit me": search a board, judge fit against the user's profile, fill each application from their facts, submit only what matches, report every application.
---

The user wants applications sent, not a list of jobs. The tedium is the forms; the judgement is whose
job fits. Both come from the user's profile: title, years, skills, city, remote or not, start date or notice period,
expected salary, work authorisation, and where the resume is (a `user` memory, or the request).

- Before planning, check that the profile is known. If title, years and city are missing, `ask_user`
  once for all of them together, and for where the resume is (Google Drive, the phone's Downloads, or
  already uploaded to the board). The resume must be reachable from the phone's file picker: a file on
  the user's computer is not, and the final answer says so.
- One step per board (LinkedIn, Indeed, Glassdoor, ZipRecruiter, Naukri in India; the user's choice,
  else LinkedIn and the country's Indeed). The step's
  goal: search with the profile's words and the filters that fit (Easy Apply, last week, remote), open
  each listing that fits by title and years, apply with the profile's facts, and report each application
  (company, title, link) and each listing skipped and why. `instructions` carry the profile's facts and
  the limits: at most N applications (the request's number, else 10), no listing below or above the
  years asked, no cover letter unless the user gave one, nothing invented.
- "Apply" is authorised by the request; each submission is still a consequential tap, so `done_when`
  names the application list, and the step reports needs_input when a form asks something the profile
  does not settle (salary where none is known, a question about relocation). Answer it, add it to the
  profile with `context` for the next step, and rerun.
- A board's "Apply on company site" opens an outside form with its own account: skip it and list it,
  unless the request says to go through.
- Finish with the applications sent (company, title, board), the ones skipped and why, and the questions
  that need the user's answer for next time; suggest saving those answers so the next run asks nothing.
