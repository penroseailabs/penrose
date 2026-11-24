import { useEffect, useState } from "react";
import { BellOff, Search, X } from "lucide-react";
import type { Briefing } from "../api";
import { origin, useBriefedApps } from "../lib/forYou";
import { clock } from "../lib/status";
import AnswerOptions from "./AnswerOptions";
import Swipe, { type SwipeAction } from "./Swipe";
import type { Workspace } from "../lib/useWorkspace";

/** An app's mark: there are no app icons on the computer, so its first letter on a small tile. */
export function AppMark({ label }: { label: string }) {
  return (
    <span className="app-mark" aria-hidden="true">
      {(label || "?").slice(0, 1).toUpperCase()}
    </span>
  );
}

/** The list's part of the sidebar: the briefings, each a line that opens it. The chats that wait for an answer
 * are in the task list below, with their dot. */
export function ForYouSide({ ws, onPicked }: { ws: Workspace; onPicked?: () => void }) {
  const { ready, unseen } = ws.forYou;
  if (!ready.length) return null;
  return (
    <section className="side-section for-you-side" aria-label="For you">
      <div className="side-section-head">
        <span className="side-section-title">
          For you
          {!!unseen && <span className="badge" aria-label={`${unseen} not opened yet`}>{unseen}</span>}
        </span>
      </div>
      <div className="list">
        {ready.map((item) => (
          <button
            key={item.id}
            className={`task briefing ${item.chat_id && item.chat_id === ws.chatId ? "active" : ""} ${item.seen ? "" : "unseen"}`}
            title={`${origin(item)}: ${item.headline}`}
            onClick={() => {
              void ws.showBriefing(item);
              onPicked?.();
            }}
          >
            <AppMark label={item.app.label} />
            <span className="t">{item.headline || item.name}</span>
            {item.status === "waiting_user" && <span className="sd wait" role="img" aria-label="Ready for you" />}
            {(item.status === "running" || item.status === "queued" || ws.opening === item.id) && (
              <span className="sd work" role="img" aria-label={ws.opening === item.id ? "Opening" : "Working"} />
            )}
          </button>
        ))}
      </div>
    </section>
  );
}

/** A swipe on the For you page, either way, sends the thing away: a notification's own gesture. */
const dismissing = (run: () => void): SwipeAction => ({ label: "Dismiss", icon: <X size={16} />, run });

/** One briefing as a card: where it is from, what happened, why it matters, and what a tap shows of it. A swipe
 * dismisses it; what penrose can do about it is in the details, and starts only when the user says so. */
function BriefingCard({ item, ws }: { item: Briefing; ws: Workspace }) {
  const working = item.status === "running" || item.status === "queued";
  const opening = ws.opening === item.id;
  const dismiss = dismissing(() => void ws.dismissBriefing(item));
  return (
    <Swipe className={`briefing-card surface ${item.seen ? "" : "unseen"} ${opening ? "opening" : ""}`} left={dismiss} right={dismiss}>
      <header>
        <AppMark label={item.app.label} />
        <span className="from">{origin(item)}</span>
        {item.count > 1 && <span className="n" title={`${item.count} notifications`} aria-label={`${item.count} notifications`}>{item.count}</span>}
        <time>{clock(item.latest)}</time>
        {/* An app's own alerts are no conversation to mute: the app is turned off in the settings. */}
        {item.name !== item.app.label && (
          <button className="ib" aria-label="Not this conversation again" title="Not this conversation again" onClick={() => ws.dismissBriefing(item, true)}>
            <BellOff size={13} />
          </button>
        )}
        <button className="ib" aria-label="Dismiss" title="Dismiss" onClick={() => ws.dismissBriefing(item)}>
          <X size={14} />
        </button>
      </header>
      <button className="briefing-main" onClick={() => void ws.showBriefing(item)}>
        <b>{item.headline || item.name}</b>
        {!!item.found && !!item.asks && <span className="asks">{item.asks}</span>}
        {(item.found || item.asks || item.why) && <span>{item.found || item.asks || item.why}</span>}
      </button>
      {!!item.options?.length && (
        <AnswerOptions options={item.options} disabled={ws.busy} onPick={(message) => void ws.answerBriefing(item, message)} />
      )}
      <div className="briefing-acts">
        {opening && <span className="briefing-state"><span className="sd work" /> Opening…</span>}
        {!opening && working && <span className="briefing-state"><span className="sd work" /> Looking into it</span>}
        {!opening && !working && (
          <button className="btn small tinted" onClick={() => void ws.showBriefing(item)}>
            See details
          </button>
        )}
      </div>
    </Swipe>
  );
}

/**
 * A thing on the page that is not a card of its own kind: it opens at a tap, goes at a swipe (or the cross, with a
 * mouse), and says what it is in a headline, a body of what it needs and a line of where it is from. The cross is a
 * sibling of the button, not inside it.
 */
function FyCard({ lead, title, body, meta, label, onOpen, onDismiss, onEnter }: {
  lead?: React.ReactNode;
  title: string;
  body?: React.ReactNode;
  meta: React.ReactNode;
  label: string;
  onOpen: () => void;
  onDismiss: () => void;
  onEnter?: () => void;
}) {
  const dismiss = dismissing(onDismiss);
  return (
    <Swipe className="fy-card surface" left={dismiss} right={dismiss}>
      <button className="fy-main" onClick={onOpen} onPointerEnter={onEnter}>
        <span className="fy-title">
          {lead}
          <b>{title}</b>
        </span>
        {body}
        <span className="fy-meta">{meta}</span>
      </button>
      <button className="ib fy-x" aria-label={`Dismiss: ${label}`} title="Dismiss" onClick={onDismiss}>
        <X size={13} />
      </button>
    </Swipe>
  );
}

/** What a thing that came in says under its headline: what is asked of the user, then the points that matter (a
 * sentence when there is one, a short list of up to three), and its own reason when the sort gave neither. */
function CameInBody({ item }: { item: Briefing }) {
  const gist = item.gist ?? [];
  if (!item.asks && !gist.length) return item.why ? <span className="fy-body">{item.why}</span> : null;
  return (
    <>
      {!!item.asks && <span className={`fy-body asks ${gist.length ? "short" : ""}`}>{item.asks}</span>}
      {gist.length === 1 && <span className={`fy-body ${item.asks ? "short" : ""}`}>{gist[0]}</span>}
      {gist.length > 1 && (
        <span className="fy-gist">
          {gist.slice(0, 3).map((line, i) => (
            <span key={i}>{line}</span>
          ))}
        </span>
      )}
    </>
  );
}

/** Past this many apps the list gets a search field. */
const SEARCH_FROM = 10;

/**
 * Before the user has chosen any app: every app the phone had notifications from this week, as a list of switches.
 * The ones where people write to the user come first, under their own heading; the rest follow, busiest first.
 */
function ChooseApps({ ws }: { ws: Workspace }) {
  const { choices, problem, setApp } = useBriefedApps();
  const [query, setQuery] = useState("");
  useEffect(() => {
    if (problem) ws.setError(problem);
  }, [problem]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!choices?.apps.length) return null;
  const words = query.trim().toLowerCase();
  const found = words
    ? choices.apps.filter((app) => app.label.toLowerCase().includes(words))
    : choices.apps;
  const groups = [
    { title: "Messages from people", apps: found.filter((app) => app.people) },
    { title: "Other apps", apps: found.filter((app) => !app.people) },
  ].filter((group) => group.apps.length);
  const chosen = choices.apps.filter((app) => app.on).length;
  return (
    <section className="for-you-setup">
      <h3>Have things ready when you arrive</h3>
      <p>Choose the apps whose messages penrose should look at as they come in. It reads and prepares; it does nothing until you say so.</p>
      <div className="app-picker">
        {choices.apps.length > SEARCH_FROM && (
          <label className="field app-search">
            <Search size={14} />
            <input
              type="search"
              aria-label="Search apps"
              placeholder={`Search ${choices.apps.length} apps`}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Escape" && query && (e.stopPropagation(), setQuery(""))}
            />
            {query && (
              <button type="button" className="ib" aria-label="Clear search" title="Clear" onClick={() => setQuery("")}>
                <X size={14} />
              </button>
            )}
          </label>
        )}
        {groups.map((group) => (
          <div key={group.title} className="app-group" role="group" aria-label={group.title}>
            <h4>{group.title}</h4>
            <div className="app-rows">
              {group.apps.map((app) => (
                <button
                  key={app.package}
                  className="app-row"
                  role="switch"
                  aria-checked={app.on}
                  aria-label={`Have things ready from ${app.label}`}
                  onClick={() => setApp(app, !app.on)}
                >
                  <AppMark label={app.label} />
                  <span className="app-name">
                    <b>{app.label}</b>
                    <small>{app.count ? `${app.count} this week` : "Quiet this week"}</small>
                  </span>
                  <span className="switch" aria-hidden="true" />
                </button>
              ))}
            </div>
          </div>
        ))}
        {!groups.length && <p className="app-none">No app called “{query.trim()}” has shown anything this week.</p>}
        {!!chosen && <p className="app-chosen">{chosen === 1 ? "1 app" : `${chosen} apps`} chosen. Change them any time in Settings.</p>}
      </div>
    </section>
  );
}

/**
 * What the empty chat shows in place of a blank page: what penrose has ready, the chats that wait for the user, and
 * what else came in. `fallback` (the generic suggestions) shows only when there is nothing at all.
 */
export function ForYouHome({ ws, fallback }: { ws: Workspace; fallback: React.ReactNode }) {
  const { ready, waiting, came_in, more, apps_on } = ws.forYou;
  // Offered while the user has chosen no app, and kept for this visit once shown: the first pick must not take it
  // away before the second.
  const [choosing, setChoosing] = useState(false);
  useEffect(() => {
    if (apps_on === 0) setChoosing(true);
  }, [apps_on]);
  const nothing = !ready.length && !waiting.length && !came_in.length;
  if (nothing)
    return (
      <>
        {fallback}
        {choosing && <ChooseApps ws={ws} />}
      </>
    );
  return (
    <div className="for-you appear">
      {!!ready.length && (
        <section>
          <h3>Ready for you</h3>
          {ready.map((item) => (
            <BriefingCard key={item.id} item={item} ws={ws} />
          ))}
        </section>
      )}
      {!!waiting.length && (
        <section>
          <h3>Waiting on you</h3>
          {waiting.slice(0, 5).map((chat) => (
            <FyCard
              key={chat.chat_id}
              lead={<span className="sd wait" />}
              title={chat.needs || chat.title}
              body={!!chat.asks && <span className="fy-body">{chat.asks}</span>}
              meta={<>{chat.needs && <span className="from">{chat.title}</span>}<time>{clock(chat.updated)}</time></>}
              label={chat.needs || chat.title}
              onOpen={() => ws.selectChat(chat.chat_id)}
              onDismiss={() => void ws.dismissWaiting(chat.chat_id)}
              onEnter={() => ws.prefetchChat(chat.chat_id)}
            />
          ))}
          {waiting.length > 5 && <p className="for-you-more">{waiting.length - 5} more in the task list</p>}
        </section>
      )}
      {!!came_in.length && (
        <section>
          <h3>Came in</h3>
          {came_in.map((item) => (
            <FyCard
              key={item.id}
              title={item.headline || item.name}
              body={<CameInBody item={item} />}
              meta={<><AppMark label={item.app.label} /><span className="from">{origin(item)}{item.count > 1 && ` · ${item.count}`}</span><time>{clock(item.latest)}</time></>}
              label={item.headline || item.name}
              onOpen={() => void ws.showBriefing(item)}
              onDismiss={() => void ws.dismissBriefing(item)}
            />
          ))}
          {!!more && <p className="for-you-more">{more} more today</p>}
        </section>
      )}
      {choosing && <ChooseApps ws={ws} />}
    </div>
  );
}
