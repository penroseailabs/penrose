import { useState } from "react";
import { File, FileText, Smartphone, RotateCw, X } from "lucide-react";
import type { Attachment } from "../api";
import { formatSize, type Purpose } from "../lib/uploads";
import { usePicture } from "./Rich";

/** A file on its way, as the composer holds it before the message is sent. */
export type Item = {
  key: string;
  name: string;
  size: number;
  purpose: Purpose;
  /** A photo's own picture while it uploads (an object URL). */
  preview?: string;
  /** 0..1: the bar that fills the pill. For the phone it covers the computer first and the phone after. */
  progress: number;
  state: "uploading" | "done" | "failed";
  error?: string;
  /** The server's id once the upload began, so Retry carries on instead of starting again. */
  id?: number;
  attachment?: Attachment;
};

/** The files waiting to go with the message: a pill each, its name and a bar that fills while it uploads. */
export function AttachPills({
  items,
  onRemove,
  onRetry,
}: {
  items: Item[];
  onRemove: (item: Item) => void;
  onRetry: (item: Item) => void;
}) {
  if (!items.length) return null;
  return (
    <div className="attach-row" role="list" aria-label="Files to send">
      {items.map((item) => {
        const phone = item.purpose === "phone";
        const where = item.attachment?.phone_path;
        const title =
          item.state === "failed"
            ? item.error || "Could not upload"
            : phone
              ? where
                ? `On the phone: ${where}`
                : "Going to the phone"
              : `${item.name} · for penrose to read`;
        return (
          <span
            key={item.key}
            role="listitem"
            className={`attach-pill ${item.state}`}
            style={{ "--p": `${Math.round(item.progress * 100)}%` } as React.CSSProperties}
            title={title}
          >
            {item.preview ? (
              <img src={item.preview} alt="" className="attach-thumb" />
            ) : phone ? (
              <Smartphone size={13} />
            ) : (
              <FileText size={13} />
            )}
            <span className="attach-name">{item.name}</span>
            {item.state === "uploading" && <span className="attach-meta">{Math.round(item.progress * 100)}%</span>}
            {item.state === "done" && phone && <span className="attach-meta">on phone</span>}
            {item.state === "failed" && (
              <button
                type="button"
                className="ib attach-act"
                onClick={() => onRetry(item)}
                aria-label={`Retry ${item.name}`}
                title={item.error || "Retry"}
              >
                <RotateCw size={12} />
              </button>
            )}
            <button
              type="button"
              className="ib attach-act"
              onClick={() => onRemove(item)}
              aria-label={`Remove ${item.name}`}
            >
              <X size={12} />
            </button>
          </span>
        );
      })}
    </div>
  );
}

/** A photo the user sent, as a thumbnail (loaded through fetch, so it also arrives over the phone app's tunnel). */
function Photo({ file }: { file: Attachment }) {
  const { url, failed } = usePicture(`/api/uploads/${file.id}/thumb`);
  const [big, setBig] = useState(false);
  if (failed) return <FileChip file={file} />;
  return (
    <span className={"you-photo" + (url ? "" : " loading") + (big ? " big" : "")}>
      {url ? (
        <img src={url} alt={file.name} onClick={() => setBig((b) => !b)} />
      ) : (
        <span className="you-photo-wait" aria-hidden="true" />
      )}
    </span>
  );
}

function FileChip({ file }: { file: Attachment }) {
  const phone = file.purpose === "phone";
  return (
    <span className="you-file" title={phone ? `On the phone: ${file.phone_path}` : file.name}>
      {phone ? <Smartphone size={14} /> : file.kind === "document" ? <FileText size={14} /> : <File size={14} />}
      <span className="you-file-text">
        <b>{file.name}</b>
        <small>
          {[formatSize(file.size), file.detail].filter(Boolean).join(" · ")}
          {phone && file.phone_path ? ` · on the phone: ${file.phone_path}` : ""}
        </small>
      </span>
    </span>
  );
}

/** The files a user message carries: photos as pictures, the rest as chips; a phone upload says where it went. */
export function MessageFiles({ files }: { files: Attachment[] }) {
  if (!files.length) return null;
  return (
    <div className="you-files">
      {files.map((file) =>
        file.kind === "image" && file.purpose === "read" ? (
          <Photo key={file.id} file={file} />
        ) : (
          <FileChip key={file.id} file={file} />
        ),
      )}
    </div>
  );
}
