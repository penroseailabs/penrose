import { BellOff } from "lucide-react";
import type { Briefing, BriefingDetail } from "../api";
import { origin } from "../lib/forYou";
import { clock } from "../lib/status";
import type { Workspace } from "../lib/useWorkspace";
import { AppMark } from "./ForYou";
import AnswerOptions from "./AnswerOptions";
import Rich from "./Rich";
import Sheet from "./Sheet";

/** A line with a quiet label over it. */
function Line({ label, children }: { label: string; children: string }) {
  return (
    <p className="bs-line">
      <small>{label}</small>
      {children}
    </p>
  );
}

/** The notifications a briefing came from, oldest first, each with who it is from and when. */
function Arrived({ detail }: { detail: BriefingDetail }) {
  return (
    <ul className="bs-arrived">
      {detail.earlier > 0 && <li className="earlier">{detail.earlier} earlier not shown</li>}
      {detail.arrived.map((n, i) => (
        <li key={i}>
          <span className="who">
            <b>{n.from}</b>
            <time>{clock(n.at)}</time>
          </span>
          {n.text}
        </li>
      ))}
    </ul>
  );
}

/**
 * What a briefing holds, shown when it is tapped anywhere: what it asks of the user, why it matters, what penrose
 * found (once it has prepared it) and the notifications themselves. Nothing starts working until the user chooses
 * "Look into it". It opens at once with what the list has; the rest fills in when the server answers.
 */
export default function BriefingSheet({ ws }: { ws: Workspace }) {
  const shown = ws.shownBriefing;
  if (!shown) return null;
  const { detail } = shown;
  // The list's own copy is the newer one (its status moves while the sheet is open).
  const live = [...ws.forYou.ready, ...ws.forYou.came_in].find((b) => b.id === shown.item.id);
  const item: Briefing = { ...(detail ?? shown.item), ...live };
  const close = ws.closeBriefing;
  const known = !!item.app;
  const opened = !!item.chat_id;
  const working = item.status === "running" || item.status === "queued";
  const opening = ws.opening === item.id;
  const gist = detail?.gist ?? item.gist ?? [];
  return (
    <Sheet title={item.headline || item.name || "Notification"} onClose={close} className="briefing">
      {/* What it holds scrolls; what can be done with it stays in reach under it. */}
      <div className="bs-body scroll">
        {known && (
          <p className="bs-meta">
            <AppMark label={item.app.label} />
            <span className="from">{origin(item)}</span>
            {item.count > 1 && <span>{item.count} notifications</span>}
            <time>{clock(item.latest)}</time>
          </p>
        )}
        {!!item.asks && <Line label="Asked of you">{item.asks}</Line>}
        {!!item.why && <Line label="Why it matters">{item.why}</Line>}
        {detail?.prepared && (
          <section className="bs-section">
            <h4>What penrose found</h4>
            <div className="relay">
              <Rich>{detail.prepared.content}</Rich>
            </div>
            {!!item.options?.length && (
              <AnswerOptions
                options={item.options}
                disabled={ws.busy}
                onPick={async (message) => {
                  if (await ws.answerBriefing(item, message)) close();
                }}
              />
            )}
          </section>
        )}
        {opened && working && (
          <p className="briefing-state">
            <span className="sd work" /> penrose is looking into it
          </p>
        )}
        {!detail?.prepared && gist.length > 0 && (
          <section className="bs-section">
            <h4>In short</h4>
            <ul className="bs-gist">
              {gist.map((line, i) => (
                <li key={i}>{line}</li>
              ))}
            </ul>
          </section>
        )}
        {!detail && <p className="bs-loading">Reading what arrived…</p>}
        {detail && detail.prepared && (
          <details className="bs-section bs-fold">
            <summary>What arrived ({detail.arrived.length + detail.earlier})</summary>
            <Arrived detail={detail} />
          </details>
        )}
        {detail && !detail.prepared && (detail.arrived.length > 0 || detail.earlier > 0) && (
          <section className="bs-section">
            <h4>What arrived</h4>
            <Arrived detail={detail} />
          </section>
        )}
      </div>
      <footer className="bs-foot">
        <button
          className="btn tinted"
          disabled={opening || (!opened && (ws.busy || ws.needsGoogle))}
          onClick={async () => {
            if (await ws.openBriefing(item)) close();
          }}
        >
          {opening ? "Starting…" : opened ? "Open chat" : "Look into it"}
        </button>
        <button
          className="btn quiet"
          onClick={() => {
            void ws.dismissBriefing(item);
            close();
          }}
        >
          Dismiss
        </button>
        {known && item.name !== item.app.label && (
          <button
            className="ib"
            aria-label="Not this conversation again"
            title="Not this conversation again"
            onClick={() => {
              void ws.dismissBriefing(item, true);
              close();
            }}
          >
            <BellOff size={15} />
          </button>
        )}
      </footer>
    </Sheet>
  );
}
