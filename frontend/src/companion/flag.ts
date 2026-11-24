/** True in the companion build (frontend/vite.config.ts, mode "companion"): the window shown on the user's own
 * phone, where there is no phone panel beside the chat and no desktop app around it. */
export const COMPANION = import.meta.env.VITE_COMPANION === "1";
