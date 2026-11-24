---
name: job-boards
description: Job boards by region: Indeed (country domains), Glassdoor, ZipRecruiter (US), Naukri (India); search by link, what a listing shows, how their apply flows differ, where the profile and resume live.
packages: com.indeed.android.jobsearch, com.glassdoor.app, com.ziprecruiter.android.release, naukriApp.appModules.login
apps: Indeed, Glassdoor, ZipRecruiter, Naukri
---

- Indeed search: `open_link(uri="https://<country>.indeed.com/jobs?q=<words>&l=<city>")` with the
  country's domain (`www.indeed.com` US, `uk.indeed.com`, `ca.indeed.com`, `de.indeed.com`,
  `in.indeed.com`; from `locale` or a user memory). ZipRecruiter:
  `https://www.ziprecruiter.com/jobs-search?search=<words>&location=<city>`. Glassdoor:
  `https://www.glassdoor.com/Job/jobs.htm?sc.keyword=<words>`. Naukri:
  `https://www.naukri.com/<words-with-dashes>-jobs-in-<city>`.
- Indeed's "Apply now" is a multi-page form with its own resume (Indeed Resume or an upload) and
  questions, ending in a Submit button; "Apply on company site" leaves the app. ZipRecruiter's
  "1-Tap Apply" sends the profile's resume at once: the tap is the consequential action. Glassdoor
  mostly hands over to Indeed or the company site. Naukri's Apply uses the profile's resume in one tap,
  sometimes after a sheet of questions.
- The profile (resume, headline, skills, salary expectation) is under the account tab; recruiters see
  it, so changes there are a task of their own, never a side effect of an application.
- Listings show salary only when the employer gave one; "Not disclosed" is a fact, not a gap to fill.
