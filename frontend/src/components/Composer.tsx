import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { TileSpinner } from "./Tiles";
import { ArrowUp, ClipboardPaste, Keyboard, ListEnd, LockKeyhole, Mic, MicOff, Paperclip, Plus, Smartphone, Square } from "lucide-react";
import type { Attachment } from "../api";
import { COMPANION } from "../companion/flag";
import { discard, isPhoto, LIMIT, formatSize, prepare, type Purpose, purposeFor, UploadError, uploadFile } from "../lib/uploads";
import type { SendExtra, Workspace } from "../lib/useWorkspace";
import { AttachPills, type Item } from "./Attachments";
import ModelsMenu from "./ModelsMenu";
import Popover from "./Popover";
import PrivateFields from "./PrivateFields";
import Waves from "../voice/Waves";
import { useDictation } from "../voice/useDictation";

export type ComposerHandle = {
  focus: () => void;
  fill: (text: string) => void;
  /** Files picked elsewhere (the voice screen's +): they wait here, uploading, to go with the next message. */
  attach: (files: File[], purpose: Purpose) => void;
};

type Props = {
  ws: Workspace;
  placeholder: string;
  /** Replies go to Relay as a message even while a private answer is pending. */
  extra?: SendExtra;
};

/**
 * Where you talk to Relay: grows with the text, Enter sends, Shift+Enter starts a new line (on the phone, Enter
 * starts a new line and only the send button sends). While a task works,
 * Enter steers it (Relay reads the message at its next step and carries on with it in mind) and Ctrl+Enter
 * queues the message to go out once the task ends. With nothing typed, the send button is a microphone: talking
 * replaces the text box with the voice's oscilloscope (same place, same size), each thing said goes to the chat as a
 * message, and the button becomes the way back to the keyboard.
 *
 * The + on the left adds files: photos and documents for penrose to read, or any file to put on the phone (it goes at
 * once, while the message is being written). Each file is a pill with its name and a bar that fills as it uploads;
 * the message goes out with them.
 */
const READABLE = "image/*,text/*,.pdf,.docx,.txt,.md,.csv,.tsv,.json,.xml,.html,.htm,.log,.yaml,.yml,.ini,.rtf";
const Composer = forwardRef<ComposerHandle, Props>(function Composer({ ws, placeholder, extra }, ref) {
  const [text, setText] = useState("");
  const [privately, setPrivately] = useState(false); // the form for giving a value privately, unasked
  const box = useRef<HTMLTextAreaElement>(null);
  // Files waiting to go with the message. Their uploads run on their own: `jobs` holds each one's promise (a send waits
  // for them), `bodies` the file as it is uploaded (a photo made smaller), `gone` the ones taken back mid-upload, and
  // `created` the chat a file made (picking a file in a new conversation creates it: that is not leaving one).
  const [items, setItems] = useState<Item[]>([]);
  const [note, setNote] = useState("");
  const [holding, setHolding] = useState(false); // a send waiting for the uploads to finish
  const jobs = useRef(new Map<string, Promise<Attachment>>());
  const sources = useRef(new Map<string, File>());
  const bodies = useRef(new Map<string, File>());
  const gone = useRef(new Set<string>());
  const created = useRef("");
  const readInput = useRef<HTMLInputElement>(null);
  const phoneInput = useRef<HTMLInputElement>(null);
  useImperativeHandle(ref, () => ({
    focus: () => box.current?.focus(),
    fill: (value) => {
      setText(value);
      requestAnimationFrame(() => box.current?.focus());
    },
    attach: (files, purpose) => void add(files, purpose),
  }));
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    el.style.height = "0px";
    el.style.height = `${Math.min(el.scrollHeight, 168)}px`;
  }, [text]);
  // A new conversation starts with an empty box, and no files.
  useEffect(() => {
    if (ws.chatId && ws.chatId === created.current) return;
    setText("");
    for (const item of itemsRef.current) if (item.preview) URL.revokeObjectURL(item.preview);
    for (const key of jobs.current.keys()) gone.current.add(key);
    jobs.current.clear();
    setItems([]);
    setNote("");
  }, [ws.chatId]);
  const itemsRef = useRef(items);
  itemsRef.current = items;
  const hasInput = !!text.trim() || items.length > 0;

  function patch(key: string, change: Partial<Item>) {
    setItems((list) => list.map((i) => (i.key === key ? { ...i, ...change } : i)));
  }
  /** Upload one file (or carry a failed one on from what arrived); the pill follows it. */
  function start(chatId: string, key: string, purpose: Purpose, resume?: number) {
    const source = sources.current.get(key) as File;
    const job = (async () => {
      let body = bodies.current.get(key);
      if (!body) {
        body = purpose === "read" ? await prepare(source) : source;
        bodies.current.set(key, body);
        patch(key, { size: body.size });
      }
      if (body.size > LIMIT[purpose])
        throw new UploadError(`${body.name} is ${formatSize(body.size)}; the most is ${formatSize(LIMIT[purpose])}.`);
      return uploadFile(chatId, body, purpose, (progress) => patch(key, { progress }), resume);
    })();
    jobs.current.set(key, job);
    job.then(
      (attachment) => {
        if (gone.current.has(key)) void discard(attachment.id);
        else patch(key, { state: "done", progress: 1, attachment, id: attachment.id });
      },
      (e: UploadError) => patch(key, { state: "failed", error: e.message, id: e.id }),
    );
  }
  /** Add files: `purpose` is what the user chose in the menu; dropped or pasted ones are told apart by their kind. */
  async function add(list: File[], purpose?: Purpose) {
    if (!list.length) return;
    setNote("");
    let chatId: string;
    try {
      chatId = await ws.ensureChat((id) => (created.current = id));
    } catch (e) {
      setNote((e as Error).message);
      return;
    }
    for (const file of list) {
      const what = purpose ?? purposeFor(file);
      const key = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      sources.current.set(key, file);
      setItems((l) => [
        ...l,
        {
          key,
          name: file.name || "file",
          size: file.size,
          purpose: what,
          preview: isPhoto(file) ? URL.createObjectURL(file) : undefined,
          progress: 0,
          state: "uploading",
        },
      ]);
      start(chatId, key, what);
    }
  }
  /**
   * Images copied to the clipboard, from the + menu. Chrome on Android gives text fields no way to receive an image
   * from the keyboard's clipboard (Gboard says "Chrome does not support image pasting here" and no paste event ever
   * fires), so the menu asks the browser for the clipboard itself: it runs on a tap, and Chrome shows its own small
   * "Paste" prompt before it hands the image over.
   */
  async function pasteImages() {
    setNote("");
    const found: File[] = [];
    try {
      for (const entry of await navigator.clipboard.read()) {
        const type = entry.types.find((t) => t.startsWith("image/"));
        if (!type) continue;
        const blob = await entry.getType(type);
        found.push(new File([blob], `pasted-${Date.now()}-${found.length + 1}.${type.slice(6).replace("jpeg", "jpg").replace(/\+.*/, "")}`, { type }));
      }
    } catch (e) {
      const denied = (e as Error).name === "NotAllowedError";
      return setNote(denied ? "penrose was not allowed to read the clipboard." : "The clipboard could not be read here.");
    }
    if (!found.length) return setNote("There is no image on the clipboard.");
    void add(found, "read");
  }
  function retry(item: Item) {
    if (!ws.chatId) return;
    patch(item.key, { state: "uploading", error: undefined, progress: 0 });
    start(ws.chatId, item.key, item.purpose, item.id);
  }
  function remove(item: Item) {
    gone.current.add(item.key);
    jobs.current.delete(item.key);
    if (item.preview) URL.revokeObjectURL(item.preview);
    if (item.id !== undefined && item.state !== "uploading") void discard(item.id);
    setItems((list) => list.filter((i) => i.key !== item.key));
  }
  function picked(purpose: Purpose) {
    return (e: React.ChangeEvent<HTMLInputElement>) => {
      const list = [...(e.target.files || [])];
      e.target.value = ""; // the same file can be picked again
      void add(list, purpose);
    };
  }

  async function submit(mode: "steer" | "next" = "steer") {
    if (!hasInput) return;
    if (items.some((i) => i.state === "failed")) return setNote("A file did not upload. Retry it or remove it.");
    setNote("");
    // Still uploading: the message goes the moment the files are there.
    const mine = items;
    setHolding(true);
    const files = await Promise.all(
      mine.map((i) => jobs.current.get(i.key)?.catch(() => null) ?? Promise.resolve(i.attachment ?? null)),
    );
    setHolding(false);
    if (files.some((f) => !f)) return setNote("A file did not upload. Retry it or remove it.");
    // Cleared at once (the message shows in the conversation straight away); back if it could not be sent.
    const sent = text;
    setText("");
    setItems([]);
    if (await ws.send(sent, { ...extra, mode, attachments: files as Attachment[] })) {
      for (const item of mine) if (item.preview) URL.revokeObjectURL(item.preview);
      return;
    }
    setText((current) => current || sent);
    setItems((current) => (current.length ? current : mine));
  }
  // Talking instead of typing: what is said goes out as it is heard, one message at a time (a send in flight holds
  // the next). If one could not be sent it is not lost: it is left in the text box, which comes back.
  const [said, setSaid] = useState<string[]>([]);
  const dictation = useDictation((words) => setSaid((list) => [...list, words]));
  useEffect(() => {
    if (!said.length || ws.busy) return;
    const [words, ...rest] = said;
    setSaid(rest);
    void ws.send(words, { ...extra, mode: "steer" }).then((sent) => {
      if (sent) return;
      dictation.stop();
      setText((current) => [current, words].filter((t) => t.trim()).join(" "));
      requestAnimationFrame(() => box.current?.focus());
    });
  }, [said, ws.busy]); // eslint-disable-line react-hooks/exhaustive-deps
  // Text appearing while the box is hidden (a reply left here, a message that did not send) brings the box back.
  useEffect(() => {
    if (text.trim() && dictation.on) dictation.stop();
  }, [text]); // eslint-disable-line react-hooks/exhaustive-deps
  // The chat's text box and what is said into it: leaving one for another stops the microphone.
  function typeInstead() {
    dictation.stop();
    requestAnimationFrame(() => box.current?.focus());
  }
  async function stop() {
    const unsent = await ws.stop();
    // Queued messages never went out: they come back here, to send or change.
    if (unsent.length) setText((current) => [...unsent, current].filter((t) => t.trim()).join("\n\n"));
  }

  return (
    <>
    {privately && (
      <div className="composer-private">
        <PrivateFields onSend={ws.sendPrivate} busy={ws.busy} onDone={() => setPrivately(false)} />
      </div>
    )}
    <form
      className="composer"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
      onDragOver={(e) => e.dataTransfer.types.includes("Files") && e.preventDefault()}
      onDrop={(e) => {
        if (!e.dataTransfer.files.length) return;
        e.preventDefault();
        void add([...e.dataTransfer.files]);
      }}
    >
      <AttachPills items={items} onRemove={remove} onRetry={retry} />
      {note && <p className="dictation-error" role="alert">{note}</p>}
      {dictation.error && !dictation.on && <p className="dictation-error" role="alert">{dictation.error}</p>}
      {dictation.on ? (
        <div className="dictation" role="img" aria-label={dictation.muted ? "Muted" : "Listening"}>
          <Waves compact muted={dictation.muted} thinking={false} />
        </div>
      ) : (
      <textarea
        ref={box}
        rows={1}
        aria-label="Message penrose"
        placeholder={placeholder}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onPaste={(e) => {
          const pasted = [...e.clipboardData.files];
          if (!pasted.length) return;
          e.preventDefault();
          void add(pasted);
        }}
        onKeyDown={(e) => {
          // On the phone Enter is a new line: the send button sends.
          if (e.key === "Enter" && !COMPANION && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            submit(ws.running && (e.ctrlKey || e.metaKey) ? "next" : "steer");
          }
        }}
      />
      )}
      <div className="ctools">
        {!dictation.on && (
          <Popover
            place="above-start"
            className="attach-menu"
            trigger={(props) => (
              <button type="button" className="ib" aria-label="Add files" title="Add photos, documents or files" {...props}>
                <Plus size={16} />
              </button>
            )}
          >
            {(close) => (
              <>
                <button type="button" role="menuitem" className="menu-item" onClick={() => (close(), readInput.current?.click())}>
                  <Paperclip size={14} />
                  <span>Photos &amp; files</span>
                  <span className="hint">for penrose to read</span>
                </button>
                {typeof navigator.clipboard?.read === "function" && (
                  <button type="button" role="menuitem" className="menu-item" onClick={() => (close(), void pasteImages())}>
                    <ClipboardPaste size={14} />
                    <span>Paste image</span>
                    <span className="hint">from the clipboard</span>
                  </button>
                )}
                <button type="button" role="menuitem" className="menu-item" onClick={() => (close(), phoneInput.current?.click())}>
                  <Smartphone size={14} />
                  <span>Upload to phone</span>
                  <span className="hint">any file</span>
                </button>
              </>
            )}
          </Popover>
        )}
        <input ref={readInput} type="file" multiple hidden accept={READABLE} onChange={picked("read")} />
        <input ref={phoneInput} type="file" multiple hidden onChange={picked("phone")} />
        <ModelsMenu ws={ws} disabled={ws.running} />
        <button
          type="button"
          className="ib"
          onClick={() => setPrivately(!privately)}
          aria-pressed={privately}
          aria-label="Send something privately"
          title="Send privately: a card, a password… The AI model sees only its name"
        >
          <LockKeyhole size={14} />
        </button>
        <span className="grow" />
        {ws.running && hasInput && (
          <button
            type="button"
            className="send queue"
            disabled={ws.busy}
            onClick={() => submit("next")}
            aria-label="Send after this task"
            title="Send after this task (Ctrl+Enter)"
          >
            <ListEnd size={14} />
          </button>
        )}
        {ws.running && !hasInput && (
          <button type="button" className="send stop" onClick={stop} aria-label="Stop" title="Stop">
            <Square size={10} fill="currentColor" />
          </button>
        )}
        {dictation.on ? (
          <>
            {/* Pausing is not leaving: the keyboard only comes up when the keyboard key is pressed. */}
            <button
              type="button"
              className={`send mic ${dictation.muted ? "is-off" : "is-live"}`}
              onClick={dictation.toggleMute}
              aria-pressed={!dictation.muted}
              aria-label={dictation.muted ? "Muted. Tap to unmute the microphone" : "Microphone on. Tap to mute"}
              title={dictation.muted ? "Unmute" : "Mute"}
            >
              {dictation.muted ? <MicOff size={15} /> : <Mic size={15} />}
            </button>
            <button type="button" className="send mode" onClick={typeInstead} aria-label="Type instead" title="Type instead">
              <Keyboard size={15} />
            </button>
          </>
        ) : !hasInput ? (
          <button
            type="button"
            className="send mic"
            disabled={ws.busy}
            onClick={dictation.start}
            aria-label="Talk instead of typing"
            title="Talk instead of typing"
          >
            <Mic size={15} />
          </button>
        ) : (
          <button
            className="send"
            disabled={!hasInput || ws.busy || holding}
            aria-label={ws.running ? "Send now" : "Send"}
            title={ws.running ? "Send now: penrose reads it at its next step (Enter)" : "Send"}
          >
            {ws.busy || holding ? <TileSpinner size={15} /> : <ArrowUp size={14} strokeWidth={2.2} />}
          </button>
        )}
      </div>
    </form>
    </>
  );
});

export default Composer;
