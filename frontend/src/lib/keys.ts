import { COMPANION } from "../companion/flag";

export type SavedKeys = Record<ProviderId, string>;
export type SaveResult = { ok: boolean; error?: string; keys?: SavedKeys };

/** Keys can be managed from the desktop window and from a linked phone; a plain browser tab has neither. */
export const canManageKeys = () => Boolean(window.relay?.keys) || COMPANION;

/** Each provider's key as its last four characters ("…abcd"), or "". The desktop window asks its app; the phone
 * asks the API over the tunnel. */
export async function loadKeys(): Promise<SavedKeys | null> {
  if (window.relay?.keys) return window.relay.keys();
  try {
    const response = await fetch("/api/keys");
    return response.ok ? await response.json() : null;
  } catch {
    return null;
  }
}

/** Checks the key with its provider, then keeps it on the computer (an empty key removes it). */
export async function saveKey(provider: ProviderId, key: string): Promise<SaveResult> {
  if (window.relay?.saveKey) return window.relay.saveKey(provider, key);
  try {
    const response = await fetch(`/api/keys/${provider}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    });
    const body = await response.json().catch(() => ({}));
    if (response.ok) return { ok: true, keys: body.keys };
    return { ok: false, error: typeof body.detail === "string" ? body.detail : "The key could not be saved." };
  } catch {
    return { ok: false, error: "Could not reach your computer. Check that penrose is running there." };
  }
}
