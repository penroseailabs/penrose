import { X } from "lucide-react";
import type { Briefing } from "../api";
import { origin } from "../lib/forYou";
import type { Workspace } from "../lib/useWorkspace";
import SwipeCard from "./SwipeCard";

type Props = {
  /** The briefings no session card already stands for. */
  items: Briefing[];
  ws: Workspace;
  /** Turn the conversation to a session (the reception desk only: useDesk.ts `answer`). */
  onTalk?: (chatId: string) => Promise<void>;
  /** Leave the voice screen for the chat. */
  onClose: () => void;
};

/** What a briefing's card says under its headline: where it is from, and what a tap does. */
function hint(item: Briefing) {
  if (!item.chat_id) return "Tap to have penrose look into it";
  return item.status === "waiting_user" ? "Ready. Tap to hear" : "Looking into it";
}

/**
 * What penrose has ready from the apps the user is briefed on (backend/app/briefings), in the voice screen's tray
 * beside the sessions' own cards. A tap opens the briefing (the first time, that has penrose look into it) and
 * turns the conversation to it; swiping left opens its chat, swiping right dismisses it.
 */
export default function BriefCards({ items, ws, onTalk, onClose }: Props) {
  const open = async (item: Briefing) => {
    const chatId = await ws.openBriefing(item);
    if (chatId && onTalk) void onTalk(chatId);
  };
  return (
    <>
      {items.map((item) => (
        <SwipeCard
          key={`brief-${item.id}`}
          className={`voice-card brief ${item.status === "waiting_user" || !item.chat_id ? "needs" : ""} ${ws.opening === item.id ? "opening" : ""}`}
          onLater={() => void ws.dismissBriefing(item)}
          onChat={async () => {
            if (await ws.openBriefing(item)) onClose();
          }}
        >
          <span className="voice-card-dot" aria-hidden="true" />
          <button className="voice-card-open" onClick={() => void open(item)}>
            <b>{item.headline || item.name}</b>
            <span>{ws.opening === item.id ? "Opening…" : `${origin(item)}. ${hint(item)}`}</span>
          </button>
          {/* Swiping is how a card goes on a phone; with a mouse there is the cross. */}
          <button className="ib voice-card-later" onClick={() => void ws.dismissBriefing(item)} aria-label={`Dismiss: ${item.headline || item.name}`} title="Dismiss">
            <X size={16} />
          </button>
        </SwipeCard>
      ))}
    </>
  );
}
