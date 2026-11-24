"""Files on a message: the chunked upload, what a task reads about them, and putting one on the phone."""

import base64
import io
import json
import zipfile

import httpx
import pytest
from app import attachments, delivery
from app.config import settings
from app.errors import DeviceError
from fakes import make_state, step_spec
from test_api import api_client


@pytest.fixture(autouse=True)
def files_under_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")


def chat(store):
    make_state(store, steps=[step_spec("a")])
    store.execute("UPDATE runs SET status='completed' WHERE id='run'")  # no task is working: a message starts one


def upload(client, name, data, purpose="read", mime="", cut=attachments.CHUNK):
    begun = client.post("/api/uploads", json={"chat_id": "chat", "name": name, "size": len(data), "mime": mime, "purpose": purpose})
    assert begun.status_code == 201, begun.text
    file_id = begun.json()["id"]
    for offset in range(0, len(data), cut):
        sent = client.post(f"/api/uploads/{file_id}/chunks",
                           json={"offset": offset, "data": base64.b64encode(data[offset:offset + cut]).decode()})
        assert sent.status_code == 200, sent.text
    return file_id


def png(size=(300, 200)):
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(out, "PNG")
    return out.getvalue()


def docx(text):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("word/document.xml",
                   '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                   f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>")
    return out.getvalue()


# --- the upload ------------------------------------------------------------------------------------------


def test_a_file_arrives_in_chunks_and_a_chunk_beyond_what_arrived_says_how_much_has(store, monkeypatch):
    chat(store)
    data = b"hello " * 100_000
    with api_client(store, monkeypatch) as client:
        begun = client.post("/api/uploads", json={"chat_id": "chat", "name": "notes.txt", "size": len(data), "purpose": "read"}).json()
        file_id = begun["id"]
        ahead = client.post(f"/api/uploads/{file_id}/chunks", json={"offset": 4096, "data": base64.b64encode(b"x").decode()})
        assert ahead.status_code == 409 and ahead.json()["detail"] == {"received": 0}
        half = len(data) // 2
        for offset, part in ((0, data[:half]), (half, data[half:]), (half, data[half:])):  # the last one again: harmless
            sent = client.post(f"/api/uploads/{file_id}/chunks", json={"offset": offset, "data": base64.b64encode(part).decode()})
            assert sent.status_code == 200
        assert client.get(f"/api/uploads/{file_id}").json()["received"] == len(data)
        done = client.post(f"/api/uploads/{file_id}/complete").json()
        assert done["kind"] == "document" and done["detail"] == "text file" and done["ref"] == f"file:{file_id}"
        assert client.post(f"/api/uploads/{file_id}/complete").status_code == 200  # again: the same file
    assert attachments.get(store, file_id)["text"].startswith("hello hello")


def test_a_file_that_did_not_all_arrive_is_not_complete(store, monkeypatch):
    chat(store)
    with api_client(store, monkeypatch) as client:
        file_id = client.post("/api/uploads", json={"chat_id": "chat", "name": "a.txt", "size": 10, "purpose": "read"}).json()["id"]
        client.post(f"/api/uploads/{file_id}/chunks", json={"offset": 0, "data": base64.b64encode(b"123").decode()})
        assert client.post(f"/api/uploads/{file_id}/complete").status_code == 422


def test_penrose_reads_photos_and_documents_but_any_file_goes_to_the_phone(store, monkeypatch):
    chat(store)
    with api_client(store, monkeypatch) as client:
        body = {"chat_id": "chat", "name": "app.apk", "size": 100, "mime": "application/vnd.android.package-archive"}
        assert client.post("/api/uploads", json={**body, "purpose": "read"}).status_code == 422
        assert client.post("/api/uploads", json={**body, "purpose": "phone"}).status_code == 201
        too_big = {**body, "size": attachments.PHONE_LIMIT + 1, "purpose": "phone"}
        assert client.post("/api/uploads", json=too_big).status_code == 422
        assert client.post("/api/uploads", json={**body, "chat_id": "nobody", "purpose": "phone"}).status_code == 404


def test_a_photo_becomes_a_jpeg_with_a_thumbnail(store, monkeypatch):
    chat(store)
    with api_client(store, monkeypatch) as client:
        file_id = upload(client, "red.png", png((3000, 1500)), mime="image/png")
        done = client.post(f"/api/uploads/{file_id}/complete").json()
        assert done["kind"] == "image" and done["mime"] == "image/jpeg" and max(done["w"], done["h"]) <= 1600
        thumb = client.get(f"/api/uploads/{file_id}/thumb")
        assert thumb.headers["content-type"] == "image/jpeg" and thumb.content[:2] == b"\xff\xd8"
        assert client.get(f"/api/uploads/{file_id}/nonsense").status_code == 404


def test_a_word_document_gives_up_its_text(store, monkeypatch):
    chat(store)
    with api_client(store, monkeypatch) as client:
        file_id = upload(client, "plan.docx", docx("Dinner at eight"))
        assert client.post(f"/api/uploads/{file_id}/complete").json()["detail"] == "Word document"
    assert "Dinner at eight" in attachments.get(store, file_id)["text"]


def test_names_are_one_plain_component():
    assert attachments.safe_name("../../etc/passwd") == "passwd"
    assert attachments.safe_name("C:\\Users\\x\\a<b>.pdf") == "ab.pdf"
    assert attachments.safe_name("") == "file"
    assert len(attachments.safe_name("a" * 300 + ".pdf")) <= 120


def test_a_file_nobody_finished_is_dropped_after_a_day(store, monkeypatch):
    chat(store)
    with api_client(store, monkeypatch) as client:
        stale = client.post("/api/uploads", json={"chat_id": "chat", "name": "a.txt", "size": 5, "purpose": "read"}).json()["id"]
        store.execute("UPDATE attachments SET created=created-100000 WHERE id=?", (stale,))
        client.post("/api/uploads", json={"chat_id": "chat", "name": "b.txt", "size": 5, "purpose": "read"})
    assert attachments.get(store, stale) is None


# --- the message -----------------------------------------------------------------------------------------


def ready(client, name, data, purpose="read", mime=""):
    file_id = upload(client, name, data, purpose, mime)
    done = client.post(f"/api/uploads/{file_id}/complete")
    assert done.status_code == 200, done.text
    return file_id


def test_a_message_with_files_starts_a_task_that_reads_a_line_on_each(store, monkeypatch):
    chat(store)
    with api_client(store, monkeypatch) as client:
        photo = ready(client, "menu.png", png(), mime="image/png")
        sent = client.post("/api/chats/chat/messages", json={"content": "What is good here?", "attachments": [photo]})
        assert sent.status_code == 202
        state = sent.json()
        assert state["request"].startswith("What is good here?\n\n📎 menu.png (photo, file:%d)" % photo)
        assert state["attached"] == [photo]
        message = client.get("/api/chats/chat").json()["messages"][-1]
        assert message["content"] == "What is good here?"  # the stored message keeps the user's words
        assert [a["name"] for a in message["attachments"]] == ["menu.png"]
        # Once a message carries a file it cannot be taken back, and it is in the conversation the agents read.
        assert client.delete(f"/api/uploads/{photo}").status_code == 409
    conversation = store.conversation_context("chat")["conversation"]
    assert conversation[-1]["attachments"] == [f"file:{photo} menu.png"]


def test_a_file_on_the_phone_is_told_with_its_path_and_where_the_files_app_is(store, monkeypatch):
    from app import api

    chat(store)
    pushed = []

    def handler(request):
        if request.url.path == "/devices":
            return httpx.Response(200, json=[{"id": "device-1", "connected": True}])
        pushed.append((request.url.path, request.content))
        return httpx.Response(200, json={"path": "/sdcard/Download/penrose/app.apk", "size": 2000})

    with api_client(store, monkeypatch) as client:
        monkeypatch.setattr(api.common, "gateway", httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handler)))
        file_id = upload(client, "app.apk", b"PK" * 1000, purpose="phone")
        # The upload reaches the phone when it completes, with no agent step; the window sees the path.
        done = client.post(f"/api/uploads/{file_id}/complete").json()
        assert done["phone_path"] == "/sdcard/Download/penrose/app.apk"
        assert pushed == [("/devices/device-1/files/app.apk", b"PK" * 1000)]
        sent = client.post("/api/chats/chat/messages", json={"content": "install this", "attachments": [file_id]})
    request = sent.json()["request"]
    assert f"The user uploaded app.apk (2.0 KB) to the phone: /sdcard/Download/penrose/app.apk (file:{file_id})" in request
    assert "primary%3ADownload%2Fpenrose" in request
    assert attachments.phone_files(store, "chat") == [{"ref": f"file:{file_id}", "name": "app.apk", "path": "/sdcard/Download/penrose/app.apk"}]


def test_a_phone_upload_that_cannot_reach_the_phone_is_kept_to_try_again(store, monkeypatch):
    from app import api

    chat(store)
    phone = {"up": False}

    def handler(request):
        if request.url.path == "/devices":
            return httpx.Response(200, json=[{"id": "device-1", "connected": phone["up"]}])
        return httpx.Response(200, json={"path": "/sdcard/Download/penrose/a.txt", "size": 3})

    with api_client(store, monkeypatch) as client:
        monkeypatch.setattr(api.common, "gateway", httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handler)))
        file_id = upload(client, "a.txt", b"abc", purpose="phone")
        failed = client.post(f"/api/uploads/{file_id}/complete")
        assert failed.status_code == 502 and "no phone running" in failed.json()["detail"]
        phone["up"] = True  # the window's Retry is the same call
        assert client.post(f"/api/uploads/{file_id}/complete").json()["phone_path"] == "/sdcard/Download/penrose/a.txt"


def test_files_alone_are_a_message_and_name_the_chat(store, monkeypatch):
    chat(store)
    store.execute("UPDATE chats SET title='New conversation' WHERE id='chat'")
    with api_client(store, monkeypatch) as client:
        file_id = ready(client, "receipt.png", png(), mime="image/png")
        assert client.post("/api/chats/chat/messages", json={"content": "", "attachments": []}).status_code == 422
        assert client.post("/api/chats/chat/messages", json={"attachments": [file_id]}).status_code == 202
        assert store.rows("SELECT title FROM chats WHERE id='chat'")[0]["title"] == "📎 receipt.png"
        # Another chat's file, or one that is not there, cannot ride on this chat's message.
        assert client.post("/api/chats/chat/messages", json={"content": "x", "attachments": [999]}).status_code == 422


def test_a_message_to_a_working_task_waits_with_its_files_and_the_task_reads_them_when_it_hears(store, monkeypatch):
    from app.runtime.runner import Runner

    make_state(store, steps=[step_spec("a")])
    with api_client(store, monkeypatch) as client:
        doc = ready(client, "notes.txt", b"the code is 4821")
        client.post("/api/chats/chat/messages", json={"content": "see attached", "attachments": [doc]})
        pending = client.get("/api/chats/chat").json()["pending"]
        assert json.loads(pending[0]["attachments"]) == [doc] and pending[0]["content"] == "see attached"
    state = store.load("run")
    heard = Runner(store).hear(state)
    assert heard == [f"see attached\n\n📎 notes.txt (text file, file:{doc}), attached for you to read"]
    assert state["attached"] == [doc]
    # The document's text is in what every agent reads, as the chat's files.
    docs = attachments.sent_documents(store, "chat")
    assert docs == [{"ref": f"file:{doc}", "name": "notes.txt", "text": "the code is 4821"}]


def test_a_queued_message_keeps_its_files_until_it_goes_out(store, monkeypatch):
    make_state(store, steps=[step_spec("a")])
    with api_client(store, monkeypatch) as client:
        photo = ready(client, "p.png", png(), mime="image/png")
        client.post("/api/chats/chat/messages", json={"content": "then this", "mode": "next", "attachments": [photo]})
        assert store.rows("SELECT attachments FROM messages WHERE role='user'") == []  # not in the conversation yet
    store.execute("UPDATE runs SET status='completed' WHERE id='run'")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        state = delivery.settle(db, "run")
    assert "📎 p.png (photo" in state["request"] and state["attached"] == [photo]
    sent = store.rows("SELECT content,attachments FROM messages WHERE role='user'")
    assert sent == [{"content": "then this", "attachments": json.dumps([photo])}]


def test_the_orchestrator_is_shown_a_photo_once(store, monkeypatch):
    chat(store)
    with api_client(store, monkeypatch) as client:
        photo = ready(client, "p.png", png(), mime="image/png")
        state = client.post("/api/chats/chat/messages", json={"content": "look", "attachments": [photo]}).json()
    jpegs = attachments.images_of(store, state["attached"])
    assert len(jpegs) == 1 and jpegs[0][:2] == b"\xff\xd8"
    assert attachments.images_of(store, []) == []


def test_deleting_the_chat_deletes_its_files(store, monkeypatch):
    chat(store)
    with api_client(store, monkeypatch) as client:
        file_id = ready(client, "notes.txt", b"hello")
        path = attachments.get(store, file_id)["path"]
        assert client.delete("/api/chats/chat").status_code == 200
    assert attachments.get(store, file_id) is None
    assert not attachments.folder("chat").exists() and path


# --- the phone ----------------------------------------------------------------------------------------------


def gateway_client(handler):
    return httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handler))


async def test_the_same_route_puts_a_file_on_the_phone_for_the_user_and_for_an_agent(store, tmp_path):
    chat(store)
    local = tmp_path / "menu.jpg"
    local.write_bytes(b"jpegdata")
    row_id = store.connect().__enter__().execute(
        "INSERT INTO attachments(chat_id,name,kind,size,purpose,path,done,sent,created) VALUES ('chat','menu.jpg','image',8,'read',?,1,1,0)",
        (str(local),)).lastrowid
    seen = []

    def handler(request):
        if request.url.path == "/devices":
            return httpx.Response(200, json=[{"id": "device-1", "connected": False}, {"id": "device-2", "connected": True}])
        seen.append((request.method, request.url.path, request.content))
        return httpx.Response(200, json={"path": "/sdcard/Download/penrose/menu.jpg", "size": 8})

    row = attachments.get(store, row_id)
    async with gateway_client(handler) as client:
        path = await attachments.put_on_phone(store, client, row)
        again = await attachments.put_on_phone(store, client, row)  # already there: not pushed again
    assert path == again == "/sdcard/Download/penrose/menu.jpg"
    assert seen == [("PUT", "/devices/device-2/files/menu.jpg", b"jpegdata")]
    assert attachments.get(store, row_id)["phone_path"] == path


async def test_with_no_phone_up_the_file_is_kept_and_the_user_is_told(store, tmp_path):
    chat(store)
    local = tmp_path / "a.txt"
    local.write_bytes(b"x")
    row = {"id": 1, "name": "a.txt", "path": str(local), "phone_path": ""}
    async with gateway_client(lambda request: httpx.Response(200, json=[])) as client:
        with pytest.raises(DeviceError, match="no phone running"):
            await attachments.put_on_phone(store, client, row)


async def test_the_gateway_pushes_into_one_folder_and_never_overwrites(monkeypatch):
    from app import gateway

    monkeypatch.setattr(gateway.core, "slots", {"device-1": "emulator-5554"})
    calls, existing = [], {"/sdcard/Download/penrose/a.pdf"}

    async def command_result(*args, **kwargs):
        calls.append(args)
        command = args[-1] if args[2] == "shell" else ""
        if command.startswith("[ -e "):
            path = command.split("'")[1] if "'" in command else command.split()[2]
            return (b"yes\n" if path in existing else b""), b"", 0
        return b"", b"", 0

    monkeypatch.setattr(gateway.core, "command_result", command_result)
    async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.ASGITransport(app=gateway.app)) as client:
        put = await client.put("/devices/device-1/files/a.pdf", content=b"%PDF")
        assert put.json() == {"path": "/sdcard/Download/penrose/a (2).pdf", "size": 4}
        # A name is only a name: a path in it is cut down to its last part, or does not route at all.
        sneaky = await client.put("/devices/device-1/files/..%5C..%5Csdcard%5Cx.txt", content=b"x")
        assert sneaky.json()["path"] == "/sdcard/Download/penrose/x.txt"
        assert (await client.put("/devices/device-1/files/..", content=b"x")).status_code in (404, 422)
        assert (await client.put("/devices/device-1/files/e.txt", content=b"")).status_code == 422
        assert (await client.put("/devices/nowhere/files/a.pdf", content=b"x")).status_code == 404
    pushes = [c for c in calls if "push" in c]
    assert [p[-1] for p in pushes] == ["/sdcard/Download/penrose/a (2).pdf", "/sdcard/Download/penrose/x.txt"]
    assert any("scan_file" in c[-1] for c in calls if c[2:3] == ("shell",))  # the phone's media index is told


# --- what an agent may run -------------------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["adb push big.zip /sdcard/", "adb -s emulator-5554 pull /sdcard/x .", "adb shell ls; adb pull /x y"])
async def test_adb_push_and_pull_are_not_available_to_the_agent(command):
    from app.tools.device import AdbCommand

    result = await AdbCommand(command=command).run(None)
    assert result.startswith("NOT RUN") and "Download/penrose" in result


def test_installing_and_opening_the_folder_are_not_refused():
    from app.tools.device import FILE_TRANSFER, MOVES_PHONE

    for command in ("adb shell pm install -r -g /sdcard/Download/penrose/app.apk",
                    "adb shell am start -a android.intent.action.VIEW -t vnd.android.document/directory "
                    "-d content://com.android.externalstorage.documents/document/primary%3ADownload%2Fpenrose"):
        assert not FILE_TRANSFER.search(command) and not MOVES_PHONE.search(command)


# --- the agents ---------------------------------------------------------------------------------------------------


async def test_the_gate_and_orchestrator_see_the_photo_and_the_document_and_can_put_a_file_on_the_phone(store, monkeypatch, tmp_path):
    from fakes import FakeDevices, Script, finish, orch, runner_for

    chat(store)
    store.execute("DELETE FROM runs")
    with api_client(store, monkeypatch) as client:
        photo = ready(client, "p.png", png(), mime="image/png")
        doc = ready(client, "notes.txt", b"the code is 4821")
        state = client.post("/api/chats/chat/messages", json={"content": "use these", "attachments": [photo, doc]}).json()
    pushed = []

    def handler(request):
        if request.url.path == "/devices":
            return httpx.Response(200, json=[{"id": "device-1", "connected": True}])
        pushed.append(request.url.path)
        return httpx.Response(200, json={"path": "/sdcard/Download/penrose/p.jpg", "size": 1})

    class Devices(FakeDevices):
        client = httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handler))

    put = {"tool": "put_on_phone", "files": [f"file:{photo}", "file:999"]}
    model = Script(Orchestrator=[orch(put, finish("Done."))])
    runner = runner_for(store, model, devices=Devices())
    state = store.load(state["id"])
    await runner.run(state)

    gate, orchestrator = model.calls[0], next(c for c in model.calls if c["agent"] == "Orchestrator")
    assert [len(c["images"]) for c in (gate, orchestrator)] == [1, 1]  # shown once, to each that decides
    assert orchestrator["context"]["attached_files"] == [{"ref": f"file:{doc}", "name": "notes.txt", "text": "the code is 4821"}]
    assert "attached" not in store.load(state["id"])  # looked at: not carried to the next decision
    journal = [j["line"] for j in store.load(state["id"])["journal"]]
    assert f"put file:{photo} (p.png) on the phone: /sdcard/Download/penrose/p.jpg" in journal
    assert any("could not put file:999" in line for line in journal)
    assert pushed == [f"/devices/device-1/files/p.png"]
    # The next message's agents are told where it is.
    assert runner.base_context(store.load(state["id"]))["phone_files"] == [
        {"ref": f"file:{photo}", "name": "p.png", "path": "/sdcard/Download/penrose/p.jpg"}]


async def test_the_orchestrator_puts_a_clipped_picture_on_the_phone_once_named_after_its_caption(store, monkeypatch, tmp_path):
    from fakes import FakeDevices, Script, finish, orch, runner_for

    chat(store)
    store.execute("DELETE FROM runs")
    with api_client(store, monkeypatch) as client:
        state = client.post("/api/chats/chat/messages", json={"content": "send me screenshots on Teams"}).json()
    clip = store.image(state["id"], "capture", b"jpegbytes", kind="clip", caption="Boss waistcoat sheet")
    state = store.load(state["id"])
    state["pictures"] = [{"ref": f"image:{clip}", "caption": "Boss waistcoat sheet", "kind": "screen", "step": "capture"}]
    store.save(state)
    pushed = []

    def handler(request):
        if request.url.path == "/devices":
            return httpx.Response(200, json=[{"id": "device-1", "connected": True}])
        pushed.append((request.url.path, request.content))
        return httpx.Response(200, json={"path": "/sdcard/Download/penrose/Boss waistcoat sheet.jpg", "size": 9})

    class Devices(FakeDevices):
        client = httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handler))

    put = {"tool": "put_on_phone", "files": [f"image:{clip}", "image:999"]}
    model = Script(Orchestrator=[orch(put, put, finish("Done."))])
    runner = runner_for(store, model, devices=Devices())
    await runner.run(store.load(state["id"]))

    after = store.load(state["id"])
    journal = [j["line"] for j in after["journal"]]
    assert f'put image:{clip} ("Boss waistcoat sheet") on the phone: /sdcard/Download/penrose/Boss waistcoat sheet.jpg' in journal
    assert any("could not put image:999" in line for line in journal)
    assert any(f"image:{clip} is on the phone already" in line for line in journal)  # the second call pushes nothing
    assert pushed == [("/devices/device-1/files/Boss waistcoat sheet.jpg", b"jpegbytes")]
    assert after["pictures"][0]["phone_path"] == "/sdcard/Download/penrose/Boss waistcoat sheet.jpg"


def test_a_database_from_before_files_were_marked_sent_gets_the_column(tmp_path):
    import sqlite3

    from app.store import Store

    db = sqlite3.connect(tmp_path / "agent.sqlite3")
    db.execute("CREATE TABLE attachments(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id TEXT,name TEXT,mime TEXT,kind TEXT,"
               "size INTEGER,purpose TEXT,path TEXT,phone_path TEXT,w INTEGER DEFAULT 0,h INTEGER DEFAULT 0,text TEXT,"
               "note TEXT,created REAL,done INTEGER DEFAULT 0)")
    db.commit()
    db.close()
    store = Store(tmp_path)
    assert store.rows("SELECT sent FROM attachments") == []


def test_a_recording_penrose_kept_is_on_its_message_and_read_back_in_parts(store, monkeypatch):
    """A call's recording (runtime/call.py): a file of the chat on a message of its own, fetched a megabyte at a
    time so it also reaches the phone app, and an agent can name it."""
    from app.api import uploads

    chat(store)
    monkeypatch.setattr(uploads, "AUDIO_PART", 1000)
    path = attachments.folder("chat") / "call.wav"
    path.parent.mkdir(parents=True)
    path.write_bytes(bytes(range(250)) * 10)
    row = attachments.keep(store, "chat", path, "Call with Grace.wav", "audio/wav", "audio", "call", note="0:02")
    message = store.message("chat", "assistant", "Call with Grace · 0:02\nGrace: Hello?", source="Call", attachments=[row["id"]])
    assert message["attachments"] == [attachments.view(row)] and message["attachments"][0]["kind"] == "audio"
    with api_client(store, monkeypatch) as client:
        shown = [m for m in client.get("/api/chats/chat").json()["messages"] if m["source"] == "Call"]
        assert [f["ref"] for f in shown[0]["attachments"]] == [f"file:{row['id']}"]
        parts = [client.get(f"/api/uploads/{row['id']}/audio?part={n}") for n in range(4)]
        assert [p.status_code for p in parts] == [200, 200, 200, 404]
        assert parts[0].headers["content-type"] == "audio/wav" and b"".join(p.content for p in parts[:3]) == path.read_bytes()
    assert store.conversation("chat") == [] or all("Call with" not in m["content"] for m in store.conversation("chat"))
