"""Files on a message: photos and documents the user gives penrose to read, files the user puts on the phone, and
what penrose itself keeps for the chat (`keep`: a call's recording).

An upload (api/uploads.py) is a row here from the first chunk. It is `done` once its bytes are all there and it has
been looked at (a photo is made a JPEG, a document's text is read out, a phone file is pushed to the phone), and
`sent` once a message carries it. Every file is the same kind of thing to the agents: a `file:N` they are told
about in the message they read. Nothing here knows what an agent will do with it.

Files live under `<user dir>/uploads/<chat id>/`; the phone's copy is in one folder, `PHONE_FOLDER`, where Files,
Photos and every app's picker find it.
"""

import asyncio
import io
import json
import re
import time
import zipfile
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

from .config import settings
from .errors import DeviceError

CHUNK = 512 * 1024  # what the window sends at a time: raw, so about 700 KB as base64, well under a tunnel frame
READ_LIMIT = 25 * 1024 * 1024  # a photo or document penrose reads
PHONE_LIMIT = 300 * 1024 * 1024  # a file put on the phone
PARTIAL_DAYS = 1  # an upload nobody finished is dropped after this long
TEXT_CHARS = 40_000  # what is read out of one document
CONTEXT_CHARS = 24_000  # what the agents are given of the chat's documents together
EXCERPT_CHARS = 2_000  # of a document that no longer fits whole
MAX_FILES = 10  # on one message
PHONE_FOLDER = "/sdcard/Download/penrose"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".heif"}
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".log", ".yaml", ".yml", ".ini", ".rtf"}
DOCUMENT_EXTENSIONS = TEXT_EXTENSIONS | {".pdf", ".docx"}

# What an agent is told once a message puts files on the phone: where the folder is in Files.
FILES_HINT = (
    "The Files app opens at that folder with: adb shell am start -a android.intent.action.VIEW -t vnd.android.document/"
    "directory -d content://com.android.externalstorage.documents/document/primary%3ADownload%2Fpenrose"
)


class Rejected(ValueError):
    """An upload or a message that cannot be taken, in words for the user."""


def folder(chat_id):
    return settings.user_dir / "uploads" / chat_id


def safe_name(name):
    """A file name as one plain path component: no directories, no control characters, a sane length."""
    name = re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "", str(name or "")).replace("\\", "/").rsplit("/", 1)[-1].strip(" .")
    if len(name) > 120:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: 115 - len(ext)] + dot + ext) if dot and len(ext) < 12 else name[:120]
    return name or "file"


def extension(name):
    return Path(name).suffix.lower()


def kind_of(name, mime=""):
    if (mime or "").startswith("image/") or extension(name) in IMAGE_EXTENSIONS:
        return "image"
    if extension(name) in DOCUMENT_EXTENSIONS or (mime or "").startswith("text/"):
        return "document"
    return "file"


def ref(row):
    return f"file:{row['id']}"


def size_text(size):
    size = float(size or 0)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" or size >= 100 else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# --- the rows ------------------------------------------------------------------------------------------------


def begin(store, chat_id, name, size, mime, purpose):
    """A new upload; returns its row. Raises Rejected when this chat or purpose cannot take it."""
    name = safe_name(name)
    if purpose not in ("read", "phone"):
        raise Rejected("An upload is either for penrose to read or for the phone.")
    kind = kind_of(name, mime)
    if purpose == "read" and kind == "file":
        raise Rejected("penrose reads photos and documents (PDF, Word, text). To give it any other file, use Upload to phone.")
    if size < 1:
        raise Rejected("That file is empty.")
    limit = READ_LIMIT if purpose == "read" else PHONE_LIMIT
    if size > limit:
        raise Rejected(f"That file is {size_text(size)}; the most for this is {size_text(limit)}.")
    sweep(store)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row_id = db.execute(
            "INSERT INTO attachments(chat_id,name,mime,kind,size,purpose,created) VALUES (?,?,?,?,?,?,?)",
            (chat_id, name, (mime or "")[:100], kind, size, purpose, time.time()),
        ).lastrowid
        path = folder(chat_id) / f"{row_id}-{name}.part"
        db.execute("UPDATE attachments SET path=? WHERE id=?", (str(path), row_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return get(store, row_id)


def keep(store, chat_id, path, name, mime, kind, purpose, note=""):
    """A file penrose made for the chat (a call's recording, runtime/call.py), whole at `path` in the chat's folder:
    its row, done and sent, so it is shown, served and deleted with the chat like any other, and the agents can name
    it (`file:N`, e.g. to put it on the phone)."""
    path = Path(path)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row_id = db.execute(
            "INSERT INTO attachments(chat_id,name,mime,kind,size,purpose,path,note,created,done,sent) "
            "VALUES (?,?,?,?,?,?,?,?,?,1,1)",
            (chat_id, safe_name(name), mime, kind, path.stat().st_size, purpose, str(path), note, time.time()),
        ).lastrowid
    return get(store, row_id)


def get(store, attachment_id, chat_id=None):
    rows = store.rows("SELECT * FROM attachments WHERE id=?" + (" AND chat_id=?" if chat_id else ""),
                      (attachment_id, chat_id) if chat_id else (attachment_id,))
    return rows[0] if rows else None


def sweep(store):
    """Drop the uploads nobody finished, a day after they began."""
    old = store.rows("SELECT id,path FROM attachments WHERE done=0 AND created<?", (time.time() - PARTIAL_DAYS * 86400,))
    for row in old:
        Path(row["path"] or "").unlink(missing_ok=True)
        store.execute("DELETE FROM attachments WHERE id=?", (row["id"],))


def received(row):
    path = Path(row["path"] or "")
    return path.stat().st_size if path.exists() else 0


def write_chunk(row, offset, data):
    """One chunk, at `offset`: a chunk already there is written again, one beyond what has arrived is refused
    (Rejected, with how much has). Returns how much has arrived."""
    have = received(row)
    if offset > have:
        raise Rejected(f"received:{have}")
    if offset + len(data) > row["size"]:
        raise Rejected("That is more data than the file's size.")
    with open(row["path"], "r+b") as f:
        f.seek(offset)
        f.write(data)
    return max(have, offset + len(data))


def view(row):
    """A file as the window reads it."""
    return {
        "id": row["id"], "ref": ref(row), "name": row["name"], "kind": row["kind"], "mime": row["mime"],
        "size": row["size"], "purpose": row["purpose"], "phone_path": row["phone_path"] or None,
        "w": row["w"], "h": row["h"], "detail": row["note"] or "",
    }


def views(store, ids):
    """The files of one message (a JSON list of ids, or None), in order."""
    ids = json.loads(ids) if isinstance(ids, str) else (ids or [])
    if not ids:
        return []
    found = {r["id"]: r for r in store.rows(f"SELECT * FROM attachments WHERE id IN ({','.join('?' for _ in ids)})", ids)}
    return [view(found[i]) for i in ids if i in found]


def rows_of(db, ids):
    """The files a message carries (a JSON list of ids, or None), as rows, in order."""
    ids = json.loads(ids) if isinstance(ids, str) else (ids or [])
    if not ids:
        return []
    found = {r["id"]: dict(r) for r in db.execute(f"SELECT * FROM attachments WHERE id IN ({','.join('?' for _ in ids)})", ids)}
    return [found[i] for i in ids if i in found]


def views_for(store, messages):
    """Each message dict gets its `attachments` as the window reads them (a list, empty for most), in one query."""
    wanted = {i for m in messages for i in json.loads(m.get("attachments") or "[]")}
    found = {}
    if wanted:
        found = {r["id"]: view(r) for r in store.rows(
            f"SELECT * FROM attachments WHERE id IN ({','.join('?' for _ in wanted)})", list(wanted))}
    for m in messages:
        m["attachments"] = [found[i] for i in json.loads(m.get("attachments") or "[]") if i in found]
    return messages


def brief(store, ids):
    """The files a message carried, as the agents read them in the conversation: `file:3 menu.jpg`, and where on
    the phone a file went."""
    ids = json.loads(ids) if isinstance(ids, str) else (ids or [])
    found = {r["id"]: r for r in store.rows(f"SELECT * FROM attachments WHERE id IN ({','.join('?' for _ in ids)})", ids)} if ids else {}
    return [f"{ref(found[i])} {found[i]['name']}" + (f" (on the phone: {found[i]['phone_path']})" if found[i]["phone_path"] else "")
            for i in ids if i in found]


def claim(db, chat_id, ids):
    """The rows of the files a message carries, in the order given, checked: each belongs to this chat and is done.
    Raises Rejected."""
    ids = list(dict.fromkeys(ids))
    if len(ids) > MAX_FILES:
        raise Rejected(f"A message can carry {MAX_FILES} files at most.")
    if not ids:
        return []
    found = {r["id"]: dict(r) for r in db.execute(
        f"SELECT * FROM attachments WHERE chat_id=? AND id IN ({','.join('?' for _ in ids)})", (chat_id, *ids))}
    missing = [i for i in ids if i not in found or not found[i]["done"]]
    if missing:
        raise Rejected("A file on this message is not ready or no longer exists.")
    return [found[i] for i in ids]


def mark_sent(db, ids):
    if ids:
        db.execute(f"UPDATE attachments SET sent=1 WHERE id IN ({','.join('?' for _ in ids)})", ids)


# --- what an agent reads about them -----------------------------------------------------------------------


def note(rows):
    """The lines a task reads with the user's words: one per file, the same for every kind."""
    lines, on_phone = [], False
    for row in rows:
        if row["purpose"] == "phone":
            on_phone = True
            lines.append(f"📎 The user uploaded {row['name']} ({size_text(row['size'])}) to the phone: "
                         f"{row['phone_path']} ({ref(row)})")
        else:
            what = "photo" if row["kind"] == "image" else (row["note"] or "document")
            lines.append(f"📎 {row['name']} ({what}, {ref(row)}), attached for you to read")
    if on_phone:
        lines.append(FILES_HINT)
    return "\n".join(lines)


def with_note(content, rows):
    """What the task is given: the user's words, then what they attached."""
    text = note(rows)
    return "\n\n".join(part for part in (content.strip(), text) if part) if text else content


def title_of(content, rows):
    """A chat's first title: the words, or the first file when there are none."""
    return content.strip() or (f"📎 {rows[0]['name']}" if rows else "")


def sent_documents(store, chat_id):
    """The text of the chat's documents for the agents: newest in full while it fits, the rest by excerpt."""
    rows = store.rows(
        "SELECT id,name,note,text FROM attachments WHERE chat_id=? AND purpose='read' AND kind='document' AND sent=1 "
        "AND text<>'' ORDER BY id DESC", (chat_id,))
    out, budget = [], CONTEXT_CHARS
    for row in rows:
        text = row["text"]
        if len(text) > budget:
            text = text[: max(EXCERPT_CHARS, budget)]
        budget = max(0, budget - len(text))
        out.append({"ref": f"file:{row['id']}", "name": row["name"], "text": text,
                    **({"cut": True} if len(text) < len(row["text"]) else {})})
    return out[::-1]


def phone_files(store, chat_id):
    """The chat's files that are on the phone, for the agents' briefs."""
    return [{"ref": f"file:{r['id']}", "name": r["name"], "path": r["phone_path"]} for r in store.rows(
        "SELECT id,name,phone_path FROM attachments WHERE chat_id=? AND sent=1 AND phone_path<>'' ORDER BY id",
        (chat_id,))]


def images_of(store, ids):
    """The JPEGs of the photos among files `ids` that are for reading, for a model to look at."""
    if not ids:
        return []
    rows = store.rows(f"SELECT id,path FROM attachments WHERE id IN ({','.join('?' for _ in ids)}) AND purpose='read' "
                      "AND kind='image' AND done=1 ORDER BY id", list(ids))
    out = []
    for row in rows:
        try:
            out.append(Path(row["path"]).read_bytes())
        except OSError:
            continue
    return out


# --- looking at what arrived -------------------------------------------------------------------------------


def jpeg_of(data):
    """A photo as a JPEG for the chat and the models, turned the way it is meant to be seen and no larger than the
    models need: (bytes, (w, h)), or None when it is not a picture."""
    from PIL import Image, ImageOps

    from .runtime import pictures

    try:
        original = Image.open(io.BytesIO(data))
        if (original.format == "JPEG" and original.mode == "RGB" and max(original.size) <= pictures.MAX_SOURCE_SIDE
                and original.getexif().get(0x0112, 1) == 1):
            return data, original.size  # already a right-way-up JPEG of a good size: re-encoding would only lose detail
        image = ImageOps.exif_transpose(original)
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            flat = Image.new("RGB", image.size, (255, 255, 255))
            flat.paste(image, mask=image.getchannel("A"))
            image = flat
        else:
            image = image.convert("RGB")
        if max(image.size) > pictures.MAX_SOURCE_SIDE:
            image.thumbnail((pictures.MAX_SOURCE_SIDE,) * 2)
    except Exception:
        return None
    out = io.BytesIO()
    image.save(out, "JPEG", quality=pictures.JPEG_QUALITY, optimize=True)
    return out.getvalue(), image.size


def thumb(path, longest):
    """A JPEG of the photo at `path`, no longer than `longest` on a side."""
    from PIL import Image

    from .runtime import pictures

    image = Image.open(path)
    image.thumbnail((longest, longest))
    out = io.BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=pictures.JPEG_QUALITY, optimize=True)
    return out.getvalue()


def docx_text(path):
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs = ["".join(t.text or "" for t in p.iter(f"{w}t")) for p in root.iter(f"{w}p")]
    return "\n".join(paragraphs)


def pdf_text(path):
    """(text, pages); text is "" for a PDF that holds none (a scan)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise Rejected("This install cannot read PDFs yet.") from None
    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
        if sum(len(p) for p in pages) > TEXT_CHARS:
            break
    return "\n\n".join(p.strip() for p in pages if p.strip()), len(reader.pages)


def read_document(path):
    """(text, a few words on what it is) of a document at `path`."""
    ext = extension(path.name.removesuffix(".part"))
    if ext == ".pdf":
        text, pages = pdf_text(path)
        label = f"PDF, {pages} page{'s' if pages != 1 else ''}"
        return text[:TEXT_CHARS], label if text else f"{label}, no text in it (a scan)"
    if ext == ".docx":
        return docx_text(path)[:TEXT_CHARS], "Word document"
    data = path.read_bytes()[: TEXT_CHARS * 4]
    for encoding in ("utf-8", "utf-16", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", errors="replace")
    if ext in (".html", ".htm"):
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))
    return text[:TEXT_CHARS], "text file" if ext in TEXT_EXTENSIONS else "document"


def finish(store, row):
    """The file's bytes are all here: settle the name, look at it. Returns the changes to its row (not yet written);
    raises Rejected when it cannot be used."""
    part = Path(row["path"])
    if received(row) != row["size"]:
        raise Rejected(f"Only {size_text(received(row))} of {size_text(row['size'])} arrived. Send it again.")
    final = part.with_name(part.name.removesuffix(".part"))
    part.replace(final)
    changes = {"path": str(final), "done": 1}
    if row["purpose"] == "read" and row["kind"] == "image":
        found = jpeg_of(final.read_bytes())
        if not found:
            final.unlink(missing_ok=True)
            raise Rejected(f"{row['name']} is not a picture penrose can open.")
        jpeg, (w, h) = found
        final = final.with_suffix(".jpg")
        final.write_bytes(jpeg)
        if str(final) != changes["path"]:
            Path(changes["path"]).unlink(missing_ok=True)
        changes.update(path=str(final), w=w, h=h, size=len(jpeg), mime="image/jpeg")
    elif row["purpose"] == "read":
        try:
            text, label = read_document(final)
        except Rejected:
            final.unlink(missing_ok=True)
            raise
        except Exception:
            final.unlink(missing_ok=True)
            raise Rejected(f"penrose could not read {row['name']}. Try a PDF, Word or text file.") from None
        changes.update(text=text, note=label)
    return changes


def update(store, attachment_id, changes):
    if changes:
        store.execute(f"UPDATE attachments SET {','.join(f'{k}=?' for k in changes)} WHERE id=?",
                      (*changes.values(), attachment_id))


def delete_chat_files(db, chat_id):
    """A deleted chat's files, on the computer (the phone's copies stay: they are the user's)."""
    db.execute("DELETE FROM attachments WHERE chat_id=?", (chat_id,))
    import shutil

    shutil.rmtree(folder(chat_id), ignore_errors=True)


# --- the phone ---------------------------------------------------------------------------------------------


async def pick_slot(client, preferred=None):
    """The phone to put a file on: `preferred`, else the first one that is up. None when there is none."""
    if preferred:
        return preferred
    try:
        r = await client.get("/devices")
    except httpx.TransportError:
        return None
    return next((d["id"] for d in (r.json() if r.is_success else []) if d.get("connected")), None)


async def push(client, slot, path, name):
    """Put the file at `path` in the phone's folder (the gateway's files router). Returns the phone path.
    Raises DeviceError in words for the user."""
    async def body():
        with open(path, "rb") as f:
            while chunk := await asyncio.to_thread(f.read, 1 << 20):
                yield chunk

    try:
        r = await client.put(f"/devices/{slot}/files/{quote(name)}", content=body(), timeout=900)
    except httpx.TransportError:
        raise DeviceError("The phone service did not answer. The file is not on the phone.") from None
    if r.is_error:
        detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
        raise DeviceError(detail if isinstance(detail, str) else f"The phone could not take the file (HTTP {r.status_code}).")
    return r.json()["path"]


async def put_on_phone(store, client, row, slot=None):
    """Put a file on the phone, for the user's upload and for an agent that needs a file there: the same route.
    Returns the phone path; the file's row keeps it."""
    if row["phone_path"]:
        return row["phone_path"]
    slot = await pick_slot(client, slot)
    if not slot:
        raise DeviceError("There is no phone running to put the file on. Start the phone and try again.")
    phone_path = await push(client, slot, row["path"], row["name"])
    update(store, row["id"], {"phone_path": phone_path})
    row["phone_path"] = phone_path
    return phone_path
