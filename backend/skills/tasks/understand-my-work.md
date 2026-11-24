---
name: understand-my-work
description: "become a workplace assistant for me" / "learn what I do at work" / "handle office work on my behalf": shadow the user's working life from whatever communication apps they use at work (chat, mail, or both) to learn their role, the people around them, what they own, what is pending and how things are said; commit each pass to memory and report what was and was not read.
---

Every workplace communication app, whatever its name, offers the same surfaces: a list of conversations
ordered by recent activity, the conversation itself with its history above the latest message, a search
over everything with filters for who said it and when, the user's own profile, the people's profiles, the
files and links shared, and the recurring meetings. This recipe reads those surfaces; the orchestrator
names the app the user works in (or several) and the subagent finds the surface in it. Nothing is sent,
reacted to, edited or marked read.

The deliverable is a dated profile in memory, built one pass at a time, plus a coverage report. The
profile holds, each fact with its date and source (which conversation, who wrote it, when):
- Identity and position: title, team, company, location, hours, manager and reports, who approves what.
- People who recur: their team and function, what they are to the user (manager, report, peer,
  counterpart, stakeholder, vendor), who starts threads, how fast each side replies, tone.
- Teams and forums: the user's own team and its rituals; the groups they work in per project; the forums
  they only read.
- Projects and the user's level on each, with the quote that shows it: owns (others ask them, they sign
  off), drives (posts updates, chases), contributes (hands-on in parts), watches (cc, FYI). Next
  milestone, open decisions, who waits on whom, where the artefacts live, the last event and its date.
- Priorities, derived: who the user answers first, what their manager asks about, named deadlines, what
  sits unread; the rule behind the ranking, not only the ranking.
- Kinds of work: what the user volunteers for, the recurring chores, the cross-team coordination, the
  decisions only they make.
- How things are said: vocabulary (code names, ids, nicknames), the user's own voice with examples, which
  channel carries what, the weekly rhythm.
- Open loops: what the user promised (to whom, by when, the quote), what others promised the user, questions
  to the user without a reply.

One step per pass, in this order, each named by surface so it maps onto any app:
1. The user's own profile: name, title, team, hours, status. Minutes.
2. The conversation list: the most recent twenty or so conversations; for each, who, what, the user's
   level, last event and date.
3. The user's own messages over the last 30 days: search filtered to the user as author. Open each hit and
   read the thread around it: the request, the answer, the decision, the follow-through.
4. Messages addressed to the user over the same period: mentions, direct messages, direct questions.
5. The recurring meetings and their chats: name, cadence, organiser, attendees.
6. Depth: for each project or person still unsettled, search by name and aliases, open the threads, read
   enough to settle it; go further back only where a recent thread refers to an earlier decision.
7. Priorities and open loops, derived from the passes above.

Rules for every step:
- A search preview is a lead, not a fact. A fact is established by opening the conversation and reading
  it, with dates read off the screen (the date the app shows on the message, not "Monday"). A conversation
  opened from a search result shows the matched message with the history above it: scroll up, read, and
  scroll back as needed.
- The step returns its facts as findings in the form the memory expects: one line per person, project or
  loop, dated, with its source. `remember` those findings after each step and again before finishing, so a
  failure loses one pass, not the task. Later passes update a person or project by the same name.
- Membership of a group proves nothing about responsibility; the user's own messages and the asks
  addressed to them do.
- Keep a coverage ledger in the facts: conversations read in depth, skimmed, unread. The final answer
  shows it.
- Colleagues are third parties: keep what they are to the user and what is open between them, never their
  personal details.
- Where the app's version of a surface differs (a filter that opens a picker, a search that needs a tab
  chosen), the app's own skill or memories say how; this recipe does not.

Finish with the profile as claims with evidence and dates, the open loops, the priority ladder with its
rule, the coverage ledger, and only the questions the chats could not answer (ownership versus
contribution, handoffs, which forums are out of scope, what to hand over first). Do not ask what the chats
already answered. A later run that starts with people and projects already in memory reads what is new
since their dates and updates them, in the same passes.
