import { useEffect, useState } from "react";
import { api } from "../api";
import { COMPANION } from "../companion/flag";

/** Where the phone's location comes from: always the device the user is using Relay on, through its browser
 *  (the desktop window included), never the server or computer Relay runs on. In the companion app, the
 *  user's own phone. When the browser can't say, the phone stays where it was: no guess stands in. */
export type Where =
  | { source: "finding" }
  | { source: "browser"; accuracy: number }
  | { source: "denied" }
  | { source: "unavailable" }
  | { source: "phone"; permission: PermissionState | "unsupported" };

type Fix = { lat: number; lng: number; accuracy: number };
const EVERY = 10 * 60_000;
// How old a fix the phone app may send with a message: a minute.
const FRESH = 60_000;

function browserLocation(): Promise<Fix | "denied" | null> {
  if (!navigator.geolocation) return Promise.resolve(null);
  return new Promise((resolve) =>
    navigator.geolocation.getCurrentPosition(
      ({ coords }) => resolve({ lat: coords.latitude, lng: coords.longitude, accuracy: coords.accuracy }),
      (error) => resolve(error.code === error.PERMISSION_DENIED ? "denied" : null),
      { enableHighAccuracy: true, timeout: 15000, maximumAge: EVERY },
    ),
  );
}

/** The user's time zone and language, as this browser reports them: what the agents read for the country, dates
 *  and times, and the time zone the phone's clock goes on (`deviceId`: now, rather than at the next task). */
export function sendLocale(deviceId?: string) {
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const language = navigator.language;
  if (!timezone || !language) return Promise.resolve();
  return api("/profile/locale", "POST", { timezone, language, device: deviceId }).catch(() => {});
}

/** Whether the browser lets Relay read this phone's location, kept current as the user changes it. */
function usePermission() {
  const [permission, setPermission] = useState<PermissionState | "unsupported">("prompt");
  useEffect(() => {
    if (!COMPANION) return;
    if (!navigator.geolocation || !navigator.permissions?.query) return setPermission("unsupported");
    let status: PermissionStatus | null = null;
    const changed = () => status && setPermission(status.state);
    navigator.permissions.query({ name: "geolocation" }).then((s) => {
      status = s;
      changed();
      s.addEventListener("change", changed);
    }, () => {});
    return () => status?.removeEventListener("change", changed);
  }, []);
  return permission;
}

/**
 * A message from the companion app: the task runs where the user's own phone is. Its browser asks once, the
 * first time; after that the position is read as each message goes (a minute-old fix will do). Never holds
 * the message up for long, and never fails it: without a fix the phone simply stays where it was.
 */
export async function sendPhoneLocation(deviceId: string) {
  void sendLocale(deviceId); // whether or not the browser shares its position
  if (!navigator.geolocation) return;
  const asking = (await navigator.permissions?.query({ name: "geolocation" }).catch(() => null))?.state;
  if (asking === "denied") return;
  const fix = await Promise.race([
    new Promise<GeolocationCoordinates | null>((resolve) =>
      navigator.geolocation.getCurrentPosition(
        ({ coords }) => resolve(coords),
        () => resolve(null),
        { enableHighAccuracy: true, timeout: 4000, maximumAge: FRESH },
      ),
    ),
    // The first time the user is reading the browser's question: give them time to answer it.
    new Promise<null>((resolve) => setTimeout(() => resolve(null), asking === "granted" ? 5000 : 30_000)),
  ]);
  if (!fix) return;
  await api(`/devices/${deviceId}/location`, "POST", {
    lat: fix.latitude,
    lng: fix.longitude,
    accuracy: Math.min(Math.max(fix.accuracy, 1), 10000),
    source: "phone",
  }).catch(() => {});
}

/** A message from the main window: the phone goes back to the window's position, if a phone's fix held it,
 *  and the user's time zone and language are this browser's again. */
export function releaseToComputer(deviceId: string) {
  void sendLocale(deviceId);
  return api(`/devices/${deviceId}/location/release`, "POST").catch(() => {});
}

/** Keeps the phone where this browser is: once it connects, then every ten minutes. The companion app sends
 *  its own position with each message instead (sendPhoneLocation). */
export function usePhoneLocation(deviceId: string | undefined, connected: boolean): Where {
  const [where, setWhere] = useState<Where>({ source: "finding" });
  const permission = usePermission();
  useEffect(() => {
    if (COMPANION || !deviceId || !connected) return;
    let alive = true;
    const update = async () => {
      void sendLocale(deviceId);
      const fix = await browserLocation();
      if (!alive) return;
      if (!fix || fix === "denied") return setWhere({ source: fix || "unavailable" });
      try {
        await api(`/devices/${deviceId}/location`, "POST", {
          lat: fix.lat,
          lng: fix.lng,
          accuracy: Math.min(Math.max(fix.accuracy, 1), 10000),
        });
        if (alive) setWhere({ source: "browser", accuracy: fix.accuracy });
      } catch {
        if (alive) setWhere({ source: "unavailable" });
      }
    };
    // While the window is hidden the update waits, then catches up when it is shown.
    let due = false;
    const tick = () => {
      if (document.hidden) due = true;
      else update();
    };
    const onVisibility = () => {
      if (!document.hidden && due) {
        due = false;
        update();
      }
    };
    update();
    const timer = setInterval(tick, EVERY);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      alive = false;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [deviceId, connected]);
  // The phone app reads its position as the user comes back to it, so the fix a tap sends (sendPhoneLocation, which
  // takes one up to a minute old) is there already rather than seconds away.
  useEffect(() => {
    if (!COMPANION || permission !== "granted") return;
    const warm = () => {
      if (!document.hidden) navigator.geolocation.getCurrentPosition(() => {}, () => {}, { enableHighAccuracy: true, timeout: 15000, maximumAge: FRESH });
    };
    warm();
    document.addEventListener("visibilitychange", warm);
    return () => document.removeEventListener("visibilitychange", warm);
  }, [permission]);
  return COMPANION ? { source: "phone", permission } : where;
}
