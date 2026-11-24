/**
 * One card on the voice screen's tray (Swipe.tsx with its two actions): a swipe to the right sends it away
 * ("later"), a swipe to the left opens its chat.
 */

import type { ReactNode } from "react";
import { BellOff, MessageSquareText } from "lucide-react";
import Swipe from "../components/Swipe";

type Props = {
  className?: string;
  /** Swiped right: not now. */
  onLater: () => void;
  /** Swiped left: carry on in the chat. */
  onChat: () => void;
  children: ReactNode;
};

export default function SwipeCard({ className, onLater, onChat, children }: Props) {
  return (
    <Swipe className={className} right={{ label: "Later", icon: <BellOff size={16} />, run: onLater }} left={{ label: "Open chat", icon: <MessageSquareText size={16} />, run: onChat }}>
      {children}
    </Swipe>
  );
}
