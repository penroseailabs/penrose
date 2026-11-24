/**
 * The companion app's shell: links the phone to a computer from the QR code's URL, keeps the link, and then
 * shows the Relay window itself over the tunnel.
 */

import { lazy, Suspense, useEffect, useMemo, useState, type ReactNode } from "react";
import { generateKey, unb64 } from "./crypto";
import { attachPush } from "./push";
import { keep, type Link } from "./store";
import { installShims, parsePairing, Tunnel, type TunnelState } from "./tunnel";

// Loaded once the tunnel is in place: the window's modules ask the API as soon as they run (the desk's first
// session, the update stream), and those requests must go over the tunnel.
const App = lazy(() => import("../App"));

const IOS = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const STANDALONE = window.matchMedia("(display-mode: standalone)").matches || (navigator as { standalone?: boolean }).standalone === true;

function phoneName() {
  if (/iPad/.test(navigator.userAgent)) return "iPad";
  if (IOS) return "iPhone";
  if (/Android/.test(navigator.userAgent)) {
    const model = navigator.userAgent.match(/Android [\d.]+; ([^;)]+)\)/)?.[1];
    return model && model !== "K" ? model.trim() : "Android phone";
  }
  return "Browser";
}

type InstallPrompt = Event & { prompt: () => Promise<void>; userChoice: Promise<{ outcome: string }> };

/** The browser's install prompt, when it offers one (Chrome on Android). */
function useInstallPrompt() {
  const [prompt, setPrompt] = useState<InstallPrompt | null>(null);
  useEffect(() => {
    const keepIt = (event: Event) => {
      event.preventDefault();
      setPrompt(event as InstallPrompt);
    };
    window.addEventListener("beforeinstallprompt", keepIt);
    return () => window.removeEventListener("beforeinstallprompt", keepIt);
  }, []);
  return prompt;
}

/** On an iPhone, a home-screen app keeps its own storage, so the pairing must happen inside it: the manifest's
 * start URL carries the QR code's address, and the app pairs when it is first opened. */
function offerHomeScreen(startUrl: string) {
  const manifest = {
    name: "penrose",
    short_name: "penrose",
    start_url: startUrl,
    display: "standalone",
    background_color: "#0f0f10",
    theme_color: "#0f0f10",
    icons: [
      { src: new URL("/companion/icon-192.png", location.origin).href, sizes: "192x192", type: "image/png" },
      { src: new URL("/companion/icon-512.png", location.origin).href, sizes: "512x512", type: "image/png" },
      { src: new URL("/companion/icon-maskable-512.png", location.origin).href, sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
  };
  let link = document.querySelector<HTMLLinkElement>('link[rel="manifest"]');
  if (!link) {
    link = document.createElement("link");
    link.rel = "manifest";
    document.head.appendChild(link);
  }
  link.href = "data:application/manifest+json," + encodeURIComponent(JSON.stringify(manifest));
}

/** A pairing link pasted into the app (the QR code's address): opens it here, so the app links itself. iPhones give a
 * Home Screen app its own storage and do not always carry the QR code's address into it (offerHomeScreen), so the
 * user copies the link in Safari and pastes it in the app instead. */
function openPasted(text: string) {
  const at = text.indexOf("#");
  if (at < 0 || !parsePairing(text.slice(at))) return false;
  location.hash = text.slice(at);
  location.reload();
  return true;
}

function PasteLink() {
  const [text, setText] = useState("");
  const [problem, setProblem] = useState("");
  const submit = (value: string) => {
    if (!openPasted(value.trim())) setProblem("That isn’t a penrose link. Copy the whole link and paste it again.");
  };
  return (
    <form
      className="companion-paste"
      onSubmit={(e) => {
        e.preventDefault();
        submit(text);
      }}
    >
      <p>Or paste the link you copied:</p>
      <input
        className="input"
        type="url"
        inputMode="url"
        autoComplete="off"
        spellCheck={false}
        placeholder="https://…"
        aria-label="penrose link"
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setProblem("");
        }}
      />
      <button className="btn" type="submit" disabled={!text.trim()}>Link</button>
      {navigator.clipboard?.readText && (
        <button
          className="btn ghost"
          type="button"
          onClick={async () => {
            try {
              submit(await navigator.clipboard.readText());
            } catch {
              setProblem("penrose couldn’t read the clipboard. Paste the link into the box instead.");
            }
          }}
        >
          Paste link
        </button>
      )}
      {problem && <p className="companion-problem">{problem}</p>}
    </form>
  );
}

function CopyLink() {
  const [copied, setCopied] = useState(false);
  return (
    <button
      className="btn"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(location.href);
          setCopied(true);
        } catch {
          setCopied(false);
        }
      }}
    >
      {copied ? "Link copied" : "Copy link"}
    </button>
  );
}

function Screen({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="companion-screen">
      <span className="mark" aria-hidden="true" />
      <h1>{title}</h1>
      {children}
    </div>
  );
}

export default function Companion() {
  const pairing = useMemo(() => parsePairing(location.hash), []);
  const [link, setLink] = useState<Link | null | undefined>(undefined);
  const [tunnel, setTunnel] = useState<Tunnel | null>(null);
  const [state, setState] = useState<TunnelState>("connecting");
  const [stay, setStay] = useState(false); // iPhone: keep going in the browser rather than add to the home screen
  const [installed, setInstalled] = useState(STANDALONE);
  const install = useInstallPrompt();

  useEffect(() => {
    keep.link().then((saved) => setLink(saved || null));
  }, []);

  const iosNeedsHomeScreen = !!pairing && IOS && !STANDALONE && !stay;
  useEffect(() => {
    if (iosNeedsHomeScreen) offerHomeScreen(location.href);
  }, [iosNeedsHomeScreen]);

  // Open the tunnel: to pair from the QR code, or to the computer this phone is linked to.
  useEffect(() => {
    if (link === undefined || iosNeedsHomeScreen) return;
    if (pairing && pairing.expires && pairing.expires * 1000 < Date.now()) {
      setState("expired");
      return;
    }
    const target = pairing
      ? { origin: location.origin, mailbox: pairing.mailbox, computerKey: pairing.computerKey, secret: pairing.secret }
      : link
        ? { origin: link.origin, mailbox: link.mailbox, computerKey: unb64(link.computerKey), secret: new Uint8Array() }
        : null;
    if (!target) return;
    let t: Tunnel | null = null;
    let gone = false;
    (async () => {
      let identity = await keep.identity();
      if (!identity) {
        identity = await generateKey();
        await keep.saveIdentity(identity);
      }
      if (gone) return;
      t = new Tunnel({ ...target, identity, phoneName: phoneName() });
      t.addEventListener("change", async () => {
        setState(t!.state);
        if (t!.state === "linked" && pairing) {
          const saved: Link = {
            origin: target.origin,
            mailbox: target.mailbox,
            computerKey: pairing ? btoaUrl(pairing.computerKey) : link!.computerKey,
            computerName: t!.computerName,
            phoneId: t!.phoneId,
            linked: Date.now(),
          };
          await keep.saveLink(saved);
          history.replaceState(null, "", location.pathname); // the secret is spent: keep it out of the address
          setLink(saved);
        }
        if (t!.state === "unlinked" || t!.state === "denied") {
          await keep.forgetLink();
        }
      });
      installShims(t);
      attachPush(t);
      setTunnel(t);
      t.connect();
    })();
    return () => {
      gone = true;
      t?.close();
    };
  }, [link === undefined, iosNeedsHomeScreen]); // eslint-disable-line react-hooks/exhaustive-deps

  if (link === undefined) return <div className="boot"><span className="mark" aria-hidden="true" /></div>;

  if (iosNeedsHomeScreen) {
    return (
      <Screen title={`Add penrose to your Home Screen`}>
        <p>
          First tap <b>Copy link</b>. Then tap <b>Share</b> below, choose <b>Add to Home Screen</b>, and open penrose from
          there. It links itself to <b>{pairing!.computerName}</b>; if it asks for the link, paste it.
        </p>
        <CopyLink />
        <button className="btn ghost" onClick={() => setStay(true)}>Continue in Safari instead</button>
      </Screen>
    );
  }

  if (!pairing && !link) {
    return (
      <Screen title="penrose on your phone">
        <p>Open penrose on your computer, choose <b>Link a phone</b> in Settings, and scan the code with this phone's camera.</p>
        <PasteLink />
      </Screen>
    );
  }

  const computer = tunnel?.computerName || pairing?.computerName || link?.computerName || "your computer";

  if (state === "pending") {
    return (
      <Screen title={`Allow this phone on ${computer}`}>
        <p>penrose on your computer is asking whether to allow this phone. Check that it shows the same code:</p>
        <div className="companion-code">{tunnel!.code.slice(0, 3)} {tunnel!.code.slice(3)}</div>
      </Screen>
    );
  }
  if (state === "denied") {
    return <Screen title="Not allowed"><p>This phone was not allowed on {computer}. Scan a new code to try again.</p><PasteLink /></Screen>;
  }
  if (state === "unlinked") {
    return <Screen title="No longer linked"><p>{computer} no longer knows this phone. Scan a new code in penrose's settings to link it again.</p><PasteLink /></Screen>;
  }
  if (state === "expired") {
    return <Screen title="This code has expired"><p>Show a new code in penrose's settings on your computer and scan that one.</p><PasteLink /></Screen>;
  }
  if (pairing && state !== "linked") {
    return (
      <Screen title={`Linking to ${computer}…`}>
        <p>{state === "offline" ? "penrose on your computer is not connected right now." : state === "error" ? tunnel?.error : "One moment."}</p>
      </Screen>
    );
  }
  if (pairing && !installed && install) {
    return (
      <Screen title={`Linked to ${computer}`}>
        <p>Add penrose to your home screen so it is a tap away.</p>
        <button
          className="btn"
          onClick={async () => {
            await install.prompt();
            const { outcome } = await install.userChoice;
            if (outcome === "accepted") setInstalled(true);
            else setInstalled(true); // asked once; the browser offers it again on its own
          }}
        >
          Install
        </button>
        <button className="btn ghost" onClick={() => setInstalled(true)}>Not now</button>
      </Screen>
    );
  }

  return (
    <div className={`companion ${state}`}>
      {/* The link comes back by itself (tunnel.ts): say so, nothing for the user to do. */}
      {/* Always mounted and floating over the page, so it fading in and out never moves the layout. */}
      <div className={`companion-bar ${state !== "linked" ? "show" : ""}`} role="status" aria-live="polite">
        {state !== "linked" && (tunnel?.wasLinked || link ? "Reconnecting to your session…" : `Connecting to ${computer}…`)}
      </div>

      <Suspense fallback={<div className="boot"><span className="mark" aria-hidden="true" /></div>}>
        {tunnel ? <App /> : <div className="boot"><span className="mark" aria-hidden="true" /></div>}
      </Suspense>
    </div>
  );
}

function btoaUrl(bytes: Uint8Array) {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
