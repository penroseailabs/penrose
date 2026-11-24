import { api, type Attachment } from "../api";

/** What the server takes at a time (backend/app/attachments.py CHUNK): about 700 KB as base64, small enough for the
 *  companion tunnel's one-frame requests. */
export const CHUNK = 512 * 1024;
export type Purpose = "read" | "phone";
/** The most for each purpose (backend/app/attachments.py). */
export const LIMIT: Record<Purpose, number> = { read: 25 * 1024 * 1024, phone: 300 * 1024 * 1024 };
/** Where the share of the bar that is the file reaching the computer ends, per purpose: the rest is the phone's. */
const ARRIVED: Record<Purpose, number> = { read: 0.95, phone: 0.7 };

const PHOTO = /\.(jpe?g|png|webp|gif|bmp|heic|heif)$/i;
const DOCUMENT = /\.(pdf|docx|txt|md|csv|tsv|json|xml|html?|log|ya?ml|ini|rtf)$/i;

export const isPhoto = (file: { name: string; type?: string }) =>
  !!file.type?.startsWith("image/") || PHOTO.test(file.name);

/** What a dropped or pasted file is for: penrose reads photos and documents; anything else goes to the phone. */
export function purposeFor(file: File): Purpose {
  return isPhoto(file) || DOCUMENT.test(file.name) || file.type.startsWith("text/") ? "read" : "phone";
}

export function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

/** A photo for penrose to read, made no larger than a model needs (a phone's camera photo is many megabytes) and
 *  turned the way it is meant to be seen. A photo the browser cannot decode goes as it is. */
export async function prepare(file: File): Promise<File> {
  if (!isPhoto(file) || file.type === "image/gif") return file;
  try {
    const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
    const scale = Math.min(1, 2048 / Math.max(bitmap.width, bitmap.height));
    if (scale === 1 && file.size < 1.5 * 1024 * 1024 && /jpe?g/.test(file.type)) return file;
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    const context = canvas.getContext("2d");
    if (!context) return file;
    context.fillStyle = "#fff"; // transparency on white, as the server does
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    bitmap.close();
    const blob = await new Promise<Blob | null>((done) => canvas.toBlob(done, "image/jpeg", 0.88));
    if (!blob) return file;
    return new File([blob], file.name.replace(/\.[^.]+$/, "") + ".jpg", { type: "image/jpeg" });
  } catch {
    return file;
  }
}

async function base64(blob: Blob) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let text = "";
  for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(text);
}

/** An upload that stopped after the server knew of it: `id` lets Retry carry on from what has arrived. */
export class UploadError extends Error {
  constructor(
    message: string,
    public id?: number,
  ) {
    super(message);
  }
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Send `file` to the server in chunks (backend/app/api/uploads.py) and finish it; for the phone, finishing puts it on
 * the phone. Works the same from the window and over the companion tunnel. A chunk that fails is tried again from
 * what the server says has arrived. `onProgress(0..1)` covers both legs for a file going to the phone. `resume`: the
 * id of an upload that already began, to carry on instead of starting again.
 */
export async function uploadFile(
  chatId: string,
  file: File,
  purpose: Purpose,
  onProgress: (fraction: number) => void,
  resume?: number,
): Promise<Attachment> {
  let id = resume;
  try {
    if (id === undefined) {
      const begun = await api<{ id: number }>("/uploads", "POST", {
        chat_id: chatId,
        name: file.name || "file",
        size: file.size,
        mime: file.type,
        purpose,
      });
      id = begun.id;
    }
    let offset = (await api<{ received: number }>(`/uploads/${id}`)).received;
    let failures = 0;
    while (offset < file.size) {
      onProgress((offset / file.size) * ARRIVED[purpose]);
      try {
        const data = await base64(file.slice(offset, offset + CHUNK));
        offset = (await api<{ received: number }>(`/uploads/${id}/chunks`, "POST", { offset, data })).received;
        failures = 0;
      } catch (e) {
        if (++failures > 4) throw e;
        await sleep(600 * failures);
        // Whether the chunk got there is the server's to say.
        offset = (await api<{ received: number }>(`/uploads/${id}`).catch(() => ({ received: offset }))).received;
      }
    }
    onProgress(ARRIVED[purpose]);
    const done = await api<Attachment>(`/uploads/${id}/complete`, "POST");
    onProgress(1);
    return done;
  } catch (e) {
    throw new UploadError((e as Error).message, id);
  }
}

/** Take back a file nobody sent: gone from the computer and, for the phone, from its folder. */
export function discard(id: number) {
  return api(`/uploads/${id}`, "DELETE").catch(() => {});
}
