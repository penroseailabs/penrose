"""Highways (app/highways): App data read from an app's own database, here a small Teams-shaped one, through the
gateway's read-only query runner (gateway/app_data.run_queries)."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from app import highways
from app.gateway import app_data
from app.highways import teams

ME_OID = "11111111-2222-3333-4444-555555555555"
ME = f"8:orgid:{ME_OID}"
ANA, BEN, CAI = "8:orgid:ana", "8:orgid:ben", "8:orgid:cai"
GROUP = "19:group1@thread.v2"
ONE = f"19:{ME_OID}_ana@unq.gbl.spaces"
NAMED = "19:named@thread.v2"
TEAM = "19:engteam@thread.tacv2"
RELEASES = "19:releases@thread.tacv2"
DAY = 86_400_000
NOW = 1_794_000_000_000

SCHEMA = """
create table ChatConversation (conversationId text primary key, topic text, consumptionHorizon text, lastMessageId text,
  lastMessageArrivalTime integer, lastMessageSequenceId integer, lastMessageSequenceIdAtSync integer,
  totalMemberCount integer, isDeleted integer, leftConversation integer);
create table Conversation (conversationId text primary key, displayName text, parentConversationId text,
  isFavorite integer, consumptionHorizon text, lastMessageArrivalTime integer, lastMessageSequenceId integer,
  lastMessageSequenceIdAtSync integer, isDeleted integer, leftConversation integer);
create table Message (id integer primary key, conversationId text, messageId text, arrivalTime integer, "from" text,
  userDisplayName text, content text, messageType text, deleteTime integer, editTime text, emotionCount text,
  mentionsMe integer, parentMessageId text, subject text);
create table ThreadUser (threadId text, userId text);
create table User (mri text, displayName text, givenName text);
create table ActivityFeed (activityType text, activitySubtype text, activityTimestamp integer, isRead integer,
  sourceThreadId text, sourceThreadTopic text, sourceUserImDisplayName text, messagePreview text, isDismissed integer);
create table MessagePropertyAttribute (messageId text, conversationId text, attributeName text, attributeValue text);
create table MessageSyncState (conversationId text, lastSyncCheckTime integer);
"""


def build(path, synced_group=True):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    users = [(ME, "Pat Doe", "Pat"), (ANA, "Ana Lima", "Ana"), (BEN, "Ben Cole", "Ben"), (CAI, "Cai Wu", "Cai")]
    db.executemany("insert into User values (?,?,?)", users)
    for conv, people in ((GROUP, (ME, ANA, BEN, CAI)), (ONE, (ME, ANA)), (NAMED, (ME, BEN))):
        db.executemany("insert into ThreadUser values (?,?)", [(conv, u) for u in people])
    messages = [
        (GROUP, "1", NOW - 3 * DAY, ANA, "Ana Lima", "<p>Kickoff notes are in the shared folder now</p>", "RichText/Html", 0, None, 0),
        (GROUP, "2", NOW - 2 * DAY, BEN, "Ben Cole",
         '<blockquote itemscope itemtype="http://schema.skype.com/Reply" itemid="1"><strong itemprop="mri" '
         'itemid="8:orgid:ana">Ana Lima</strong><p itemprop="preview">Kickoff notes are in</p></blockquote>'
         '<p>Thanks <span itemtype="http://schema.skype.com/Mention" itemid="0">Pat</span>, see '
         '<a href="https://x.example/f" itemtype="http://schema.skype.com/HyperLink/Files">plan.pdf</a></p>',
         "RichText/Html", 0, "1", 1),
        (GROUP, "3", NOW - DAY, CAI, "Cai Wu", "<p>removed</p>", "RichText/Html", NOW - DAY + 5, None, 0),
        (GROUP, "4", NOW - 1000, ANA, "Ana Lima", '<p>Shipping the release candidate tonight <img itemtype='
         '"http://schema.skype.com/AMSImage" src="x"></p>', "RichText/Html", 0, None, 0),
        (ONE, "10", NOW - 5 * DAY, ME, "Pat Doe", "<p>Lunch on Friday works for me</p>", "RichText/Html", 0, None, 0),
        (NAMED, "20", NOW - 6 * DAY, BEN, "Ben Cole", "<addmember><initiator>8:orgid:ben</initiator><target>"
         f"{ME}</target></addmember>", "ThreadActivity/AddMember", 0, None, 0),
    ]
    db.executemany('insert into Message (conversationId, messageId, arrivalTime, "from", userDisplayName, content, '
                   "messageType, deleteTime, editTime, mentionsMe) values (?,?,?,?,?,?,?,?,?,?)",
                   messages)
    db.execute("update Message set emotionCount=? where messageId='4'", (json.dumps({"messageId": 4, "like": 2}),))
    # A team: its General channel (followed) with a post and a reply, and a channel nobody follows.
    db.executemany("insert into Conversation values (?,?,?,?,?,?,?,?,0,0)", [
        (TEAM, "Eng Team", None, 1, f"{NOW - 10 * DAY};x;y", NOW - 2000, 2, 2),
        (RELEASES, "Releases", TEAM, 0, "", NOW - 9 * DAY, 1, 1),
    ])
    db.executemany('insert into Message (conversationId, messageId, arrivalTime, "from", userDisplayName, content, '
                   "messageType, deleteTime, mentionsMe, parentMessageId, subject) values (?,?,?,?,?,?,?,0,0,?,?)", [
        (TEAM, "30", NOW - 3000, BEN, "Ben Cole", "<p>Version two goes out on Monday</p>", "RichText/Html", "30", "Release 2.0"),
        (TEAM, "31", NOW - 2000, CAI, "Cai Wu", "<p>Docs are ready for it</p>", "RichText/Html", "30", ""),
        (RELEASES, "40", NOW - 9 * DAY, BEN, "Ben Cole", "<p>old news</p>", "RichText/Html", "40", "Old"),
    ])
    db.execute("insert into MessagePropertyAttribute values ('2', ?, 'fileName', 'plan.pdf')", (GROUP,))
    seq = 4
    chats = [
        (GROUP, None, f"{NOW - 2 * DAY};x;y", "4", NOW - 1000, seq, seq if synced_group else 0, 4),
        (ONE, None, f"{NOW};x;y", "10", NOW - 5 * DAY, 9, 9, 2),
        (NAMED, "Launch room", "0;x;y", "20", NOW - 6 * DAY, 1, 1, 2),
    ]
    db.executemany("insert into ChatConversation values (?,?,?,?,?,?,?,?,0,0)", chats)
    db.execute("insert into ActivityFeed values ('mentionInChat','person',?,0,?,NULL,'Ben Cole','Thanks Pat, see plan.pdf',0)",
               (NOW - 2 * DAY, GROUP))
    db.commit()
    db.close()


class FakeDevices:
    """The gateway's app_db over a local database: the same read-only runner, the same answer shape."""

    def __init__(self, tmp_path):
        self.copy = SimpleNamespace(path=lambda part: tmp_path / part, work_stamp=None)
        self.calls = 0

    async def app_db(self, slot, package, file, queries):
        self.calls += 1
        stamp = str(self.calls)  # the app "wrote" before every read: the working copy is rebuilt from main.db
        results = await asyncio.to_thread(app_data.run_queries, self.copy, stamp, queries)
        return {"ok": True, "file": f"SkypeTeams_{ME_OID}.db", "stamp": stamp, "utc_offset": "+0000", "results": results}


def node(**kw):
    return {"c": [], **kw}


def screen(activity, fragments, nodes=(), header_label=None):
    elements = {}
    if header_label:
        elements[1] = SimpleNamespace(label=header_label, node={"id": "infoLayout"})
    return SimpleNamespace(header={"app": teams.PACKAGE, "activity": f"com.microsoft.teams.x.{activity}",
                                   "fragments": list(fragments)},
                           elements=elements, snapshot={"windows": [{"nodes": list(nodes)}]})


def list_screen(*names):
    return screen("MainActivity", ["ConversationListFragment"], [node(id="chat_display_name", text=n) for n in names])


def chat_screen(label, *bubbles):
    return screen("ChatsActivity", ["ChatFragment"], [node(id="rich_text_layout", c=[node(text=b)]) for b in bubbles], label)


class Session:
    def __init__(self, tmp_path, scr):
        self.devices, self.slot, self.highways, self.lines = FakeDevices(tmp_path), "device-1", {}, []
        self.opened = []
        self.driver = SimpleNamespace(screen=scr, device=SimpleNamespace(key=self.key, swipe=self.swipe),
                                      gesture=lambda d: (500, 400, 500, 1400))
        self.lane = SimpleNamespace(run=self.run)
        self.swipes = 0

    async def key(self, name):
        self.opened.append(("key", name))

    async def swipe(self, *args):
        self.swipes += 1

    async def run(self, op):
        self.opened.append(("open", op["uri"]))
        return {"ok": True}

    def done(self, line):
        self.lines.append(line)

    async def device_action(self, kind, command, work):
        ok, line, _ = await work()
        return ok, line


@pytest.fixture
def phone(tmp_path):
    build(tmp_path / "main.db")
    return lambda scr: Session(tmp_path, scr)


def test_plain_reads_replies_mentions_files_and_images():
    text, reply = teams.plain(
        '<blockquote itemtype="http://schema.skype.com/Reply"><strong itemprop="mri">Ana</strong>'
        '<p itemprop="preview">Can you check?</p></blockquote><p>Done <span itemtype="http://schema.skype.com/Mention">'
        'Ana</span> &amp; more</p><p><a href="https://a.example/x">the doc</a> <img itemtype="http://schema.skype.com/'
        'AMSImage" src="y"></p>')
    assert reply == ("Ana", "Can you check?")
    assert text == "Done @Ana & more / the doc (https://a.example/x) (image)"


def test_titles_follow_teams_list():
    def m(given, display, others):
        return {"givenName": given, "displayName": display, "others": others}

    assert teams.title("Launch room", []) == "Launch room"
    assert teams.title(None, [m("Ana", "Ana Lima", 1)]) == "Ana Lima"
    assert teams.title(None, [m("Ana", "", 2), m("Ben", "", 2)]) == "Ana and Ben"
    assert teams.title(None, [m("Ana", "", 3), m("Ben", "", 3), m("Cai", "", 3)]) == "Ana, Ben, and Cai"
    assert teams.title(None, [m("Ana", "", 6), m("Ben", "", 6), m("Cai", "", 6)]) == "Ana, Ben + 4"


def test_read_only_runner_refuses_writes(tmp_path):
    build(tmp_path / "main.db")
    copy = SimpleNamespace(path=lambda part: tmp_path / part, work_stamp=None)
    got = app_data.run_queries(copy, "1", {"bad": ["delete from Message", []], "two": ["select 1; drop table User", []],
                                           "ok": ["select count(*) from Message", []]})
    assert got["bad"] == {"error": "only one SELECT per query"} and "error" in got["two"]
    assert got["ok"]["rows"] == [[9]]


def test_chat_list_block(phone):
    session = phone(list_screen("Ana, Ben, and Cai", "Ana Lima"))
    text = asyncio.run(highways.block(session))
    assert text.startswith("App data (Microsoft Teams)")
    # A group nobody named says so; the deleted message does not count as unread.
    assert '1. "Ana, Ben, and Cai" (group, no name set, 4 people, UNREAD: 1 new) | ' in text
    assert '2. "General" (channel in team Eng Team, UNREAD: 2 new) | ' in text and "Cai Wu: Docs are ready" in text
    assert "Releases" not in text  # not followed: not on the chat list
    assert "Ana Lima: Shipping the release candidate tonight (image)" in text
    assert '3. "Ana Lima" (1:1)' in text and "You: Lunch on Friday" in text
    assert '4. "Launch room" (group, 2 people) | ' in text and "(no message on the phone)" in text  # events only
    assert "mentionInChat person (UNREAD) | Ben Cole | Ana, Ben, and Cai" in text


def test_chat_list_that_does_not_match_the_screen_falls_back(phone):
    session = phone(list_screen("Someone Else", "Another Chat", "Third"))
    text = asyncio.run(highways.block(session))
    assert "not available this turn" in text and "do not match" in text
    assert session.lines  # said in the session's record too


def test_synced_chat_shows_its_messages_whole(phone):
    session = phone(chat_screen("Ana, Ben, and Cai · 4 participants", "Shipping the release candidate tonight"))
    text = asyncio.run(highways.block(session))
    assert 'Chat on screen: "Ana, Ben, and Cai" (group' in text
    assert "The phone has the whole chat (4 messages and events), synced to the newest." in text
    assert ('Ben Cole: Thanks @Pat, see (file: plan.pdf) [replying to Ana Lima: "Kickoff notes are in"; edited; '
            "mentions you]") in text
    assert "Cai Wu: (deleted message)" in text
    assert "[reactions: like 2]" in text


def test_unsynced_chat_says_so(tmp_path):
    build(tmp_path / "main.db", synced_group=False)
    session = Session(tmp_path, chat_screen("Ana, Ben, and Cai", "Shipping the release candidate tonight"))
    text = asyncio.run(highways.block(session))
    assert "NOT synced" in text and 'app_action open_chat text="Ana, Ben, and Cai"' in text
    assert "Shipping the release candidate" in text  # what it has around the message on screen


def test_changed_database_falls_back(tmp_path):
    build(tmp_path / "main.db")
    db = sqlite3.connect(tmp_path / "main.db")
    db.execute("alter table Message rename column mentionsMe to mentionsUser")
    db.commit()
    db.close()
    session = Session(tmp_path, list_screen("Ana Lima"))
    text = asyncio.run(highways.block(session))
    assert "not available this turn" in text and "Message.mentionsMe missing" in text


def test_open_chat_by_name_leaves_an_open_chat_first(phone):
    session = phone(chat_screen("Ana Lima", "Lunch on Friday works for me"))
    line = asyncio.run(highways.act(session, "open_chat", 0, "launch room"))
    assert line.startswith('opened "Launch room" (group); Teams synced its newest messages')
    assert session.opened[0] == ("key", "back")  # a link sent from inside a chat does not navigate
    assert session.opened[1] == ("open", teams.LINK.format(NAMED))


def test_open_chat_names_the_choices_when_several_match(phone):
    session = phone(list_screen("Ana Lima"))
    line = asyncio.run(highways.act(session, "open_chat", 0, "a"))
    assert line.startswith("NOT RUN: several match")


def test_older_shows_the_phones_earlier_messages_first(phone, monkeypatch):
    monkeypatch.setattr(teams, "MESSAGES_SHOWN", 2)
    session = phone(chat_screen("Ana, Ben, and Cai", "Shipping the release candidate tonight"))
    first = asyncio.run(highways.block(session))
    assert "2 earlier ones on the phone are not shown" in first
    line = asyncio.run(highways.act(session, "older"))
    assert line.startswith("App data now shows")
    second = asyncio.run(highways.block(session))
    assert "Kickoff notes" in second and "Shipping the release" not in second
    assert session.swipes == 0  # the phone had them: nothing scrolled
    assert asyncio.run(highways.act(session, "older")) == "nothing older: App data already shows the start of the conversation"


def channel_screen(label, *bubbles):
    return screen("ConversationsActivity", ["ConversationsWithComposeFragment"],
                  [node(id="message_content", c=[node(text=b)]) for b in bubbles], label)


def test_channel_shows_posts_and_replies(phone):
    session = phone(channel_screen("Eng Team", "Docs are ready for it"))
    text = asyncio.run(highways.block(session))
    assert 'Channel on screen: "General" (channel in team Eng Team)' in text
    assert "Ben Cole: [Release 2.0] Version two goes out on Monday" in text
    assert 'Cai Wu: Docs are ready for it [reply to the post "Release 2.0"]' in text


def test_open_channel_by_its_link(phone):
    session = phone(list_screen("Ana Lima"))
    line = asyncio.run(highways.act(session, "open_chat", 0, "releases"))
    assert line.startswith('opened "Releases" (channel in team Eng Team)')
    assert session.opened == [("open", teams.CHANNEL_LINK.format("19%3Areleases%40thread.tacv2", "Releases"))]


def test_unread_that_differs_from_teams_rows_is_named(phone):
    scr = list_screen("Ana, Ben, and Cai", "Ana Lima")
    scr.elements[5] = SimpleNamespace(label="Ana Lima. Chat. New messages. Last message at Sunday",
                                      node={"id": "chat_item_container"})
    scr.elements[6] = SimpleNamespace(label="Ana, Ben, and Cai. Group chat. New messages. Last message",
                                      node={"id": "chat_item_container"})
    text = asyncio.run(highways.block(phone(scr)))
    assert 'show unread differently for: "Ana Lima" (Teams: unread). For those, go by Screen.' in text


def test_chat_list_shows_unread_messages_without_opening(phone):
    text = asyncio.run(highways.block(phone(list_screen("Ana Lima"))))
    assert "its unread messages, oldest first (1 of 1; read here, they stay unread in Teams):" in text
    assert "Ana Lima: Shipping the release candidate tonight (image)" in text.split("its unread messages", 1)[1]
    assert "Cai Wu: Docs are ready for it" in text.split("(2 of 2;", 1)[1]


def test_chat_list_pages_through_every_chat(phone, monkeypatch):
    monkeypatch.setattr(teams, "CHATS_SHOWN", 2)
    monkeypatch.setattr(teams, "CHATS_PAGE", 2)
    session = phone(list_screen("Ana Lima"))
    first = asyncio.run(highways.block(session))
    assert "newest first: 1-2 of 4, all that are on the phone" in first and "app_action older shows the next 2" in first
    assert asyncio.run(highways.act(session, "older")) == "App data now lists chats 3-4 of 4"
    second = asyncio.run(highways.block(session))
    assert '3. "Ana Lima" (1:1)' in second and '4. "Launch room"' in second and "(the end of the list" in second
    # The page before stays, as short rows: nothing seen has to be paged back to.
    assert "1-2, shown before, are listed first as short rows" in second
    assert '1. "Ana, Ben, and Cai" (group, no name set, 4 people, UNREAD: 1 new) | ' in second
    assert "Shipping the release candidate" not in second
    assert "all last active before" in first and "app_action list shows the list from its start again" in second
    assert asyncio.run(highways.act(session, "older")).startswith("nothing older: App data's chat list already reached its end")
    assert asyncio.run(highways.act(session, "list")) == "App data shows the chat list from its start"
    assert "newest first: 1-2 of 4" in asyncio.run(highways.block(session))  # the end of the list is not a dead end
    line = asyncio.run(highways.act(session, "open_chat", 4))  # numbers are the same on every page
    assert line.startswith('opened "Launch room"')


def test_read_shows_whole_chats_without_opening_them(phone, monkeypatch):
    monkeypatch.setattr(teams, "SYNC_SETTLE", 0)
    session = phone(list_screen("Ana, Ben, and Cai", "Ana Lima"))
    asyncio.run(highways.block(session))
    line = asyncio.run(highways.act(session, "read", 0, "", [1, 4]))
    assert line == """App data now shows "Ana, Ben, and Cai", "Launch room": read from the phone's data, not opened"""
    text = asyncio.run(highways.block(session))
    assert "Read for you: 2 conversations" in text
    assert ('1. "Ana, Ben, and Cai" (group, no name set, 4 people: Ana Lima, Ben Cole, Cai Wu, you). '
            "The phone has the whole chat (4 messages and events).") in text
    assert "Newest 4 of its messages and events on the phone, oldest first:" in text
    assert text.index("Kickoff notes are in the shared folder") < text.index("Shipping the release candidate tonight")
    assert '4. "Launch room" (group, 2 people: Ben Cole, you). The phone has the whole chat (1 messages' in text
    assert "(Ben Cole added you)" in text
    # The list stays below what was read, in short rows; what was read stays while more is read.
    assert text.index("Read for you") < text.index("Chats and followed channels, newest first: 1-4 of 4")
    assert '3. "Ana Lima" (1:1) | ' in text and "Lunch on Friday" not in text
    assert session.opened == [] and session.swipes == 0  # the phone held them whole: nothing was done on it


def test_read_opens_a_chat_the_phone_has_not_loaded(tmp_path):
    # The 1:1 was never opened on this phone: one message of nine, the one the chat list shows. Reading it opens it,
    # which is the only way Teams loads a chat, and leaves it again.
    build(tmp_path / "main.db")
    session = Session(tmp_path, list_screen("Ana, Ben, and Cai", "Ana Lima"))
    opened = session.run

    async def run(op):
        db = sqlite3.connect(tmp_path / "main.db")
        db.executemany('insert into Message (conversationId, messageId, arrivalTime, "from", userDisplayName, content, '
                       "messageType, deleteTime) values (?,?,?,?,?,?,?,0)",
                       [(ONE, str(50 + i), NOW - 6 * DAY - i * 1000, ANA, "Ana Lima", f"<p>Earlier message number {i}</p>",
                         "RichText/Html") for i in range(5)])
        db.execute("insert into MessageSyncState values (?, ?)", (ONE, NOW))
        db.commit()
        db.close()
        return await opened(op)

    session.lane.run = run
    asyncio.run(highways.block(session))
    line = asyncio.run(highways.act(session, "read", 0, "", [1, 3]))
    assert line == ('App data now shows "Ana, Ben, and Cai", "Ana Lima": "Ana Lima" opened so that Teams loaded it '
                    "(that marks it read in Teams); the rest read from the phone's data, not opened")
    assert session.opened == [("open", teams.LINK.format(ONE)), ("key", "back")]  # only the one that needed it
    text = asyncio.run(highways.block(session))
    # Fewer than the page Teams loads at once: that is the whole of it, whatever the counter says.
    assert ("Opened just now so that Teams loaded it: that is all Teams gives of this chat, 6 messages and events "
            "(its counter says about 9: it also counts ones it does not keep).") in text
    assert "Earlier message number 4" in text and "You: Lunch on Friday works for me" in text
    # Loaded in this session: read again later, it is not opened a second time.
    session.highways[teams.PACKAGE].reading = []
    line = asyncio.run(highways.act(session, "read", 3))
    assert line == 'App data now shows "Ana Lima": already loaded when it was opened earlier in this session'
    assert len(session.opened) == 2


def test_read_says_when_a_loaded_chat_has_older_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(teams, "LOAD_PAGE", 5)
    build(tmp_path / "main.db")
    session = Session(tmp_path, list_screen("Ana, Ben, and Cai", "Ana Lima"))
    opened = session.run

    async def run(op):  # Teams loads a full page of the 1:1: more of it is on the server
        db = sqlite3.connect(tmp_path / "main.db")
        db.executemany('insert into Message (conversationId, messageId, arrivalTime, "from", userDisplayName, content, '
                       "messageType, deleteTime) values (?,?,?,?,?,?,?,0)",
                       [(ONE, str(50 + i), NOW - 6 * DAY - i * 1000, ANA, "Ana Lima", f"<p>Earlier message number {i}</p>",
                         "RichText/Html") for i in range(5)])
        db.commit()
        db.close()
        return await opened(op)

    session.lane.run = run
    asyncio.run(highways.block(session))
    asyncio.run(highways.act(session, "read", 3))
    text = asyncio.run(highways.block(session))
    assert ("Opened just now so that Teams loaded it: the phone has its newest 6 messages and events of about 9; the "
            "older ones are NOT shown and load with open_chat, then older.") in text


def test_read_never_passes_off_a_part_as_the_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(teams, "SYNC_WAIT", 0.3)
    build(tmp_path / "main.db")
    db = sqlite3.connect(tmp_path / "main.db")
    # The group: 30 messages on the server, four on the phone, and Teams does not load it when asked (a stuck
    # phone). The named one: a newer message exists that the phone does not have.
    db.execute("update ChatConversation set lastMessageSequenceId=30, lastMessageSequenceIdAtSync=5 where conversationId=?", (GROUP,))
    db.execute("update ChatConversation set lastMessageArrivalTime=? where conversationId=?", (NOW - 4 * DAY, NAMED))
    db.commit()
    db.close()
    session = Session(tmp_path, list_screen("Ana, Ben, and Cai", "Ana Lima"))
    asyncio.run(highways.block(session))
    line = asyncio.run(highways.act(session, "read", 0, "", [1, 2, 3]))  # the group, General, Launch room
    assert '"Ana, Ben, and Cai" did not load in 0.3 s: shown as far as the phone has it' in line
    assert line.endswith("the rest read from the phone's data, not opened")
    text = asyncio.run(highways.block(session))
    assert ("NOT loaded: Teams loads a chat only when it is opened, and the phone has just 4 of about 30 messages and "
            "events of this one") in text and "What is shown is NOT the conversation: open_chat loads it." in text
    assert "The phone does NOT have this chat's newest messages (it has 1, the last from" in text
    assert '"General" (channel in team Eng Team). The phone has the whole channel (2 messages and events).' in text


def test_read_keeps_the_chats_read_last(phone, monkeypatch):
    monkeypatch.setattr(teams, "READ_CHATS", 2)
    monkeypatch.setattr(teams, "SYNC_SETTLE", 0)
    session = phone(list_screen("Ana Lima"))
    asyncio.run(highways.block(session))
    asyncio.run(highways.act(session, "read", 0, "", [1, 3]))
    line = asyncio.run(highways.act(session, "read", 4))
    assert line.endswith('beside the 1 read before; no longer shown (the ones read longest ago): "Ana, Ben, and Cai"')
    text = asyncio.run(highways.block(session))
    assert "Kickoff notes" not in text and "Lunch on Friday" in text and "(Ben Cole added you)" in text


def test_read_by_name_and_only_on_the_chat_list(phone):
    session = phone(list_screen("Ana Lima"))
    asyncio.run(highways.block(session))
    assert asyncio.run(highways.act(session, "read", 0, "launch room")).startswith('App data now shows "Launch room"')
    assert asyncio.run(highways.act(session, "read", 0, "", [9])).startswith("NOT RUN: there is no item 9")
    assert asyncio.run(highways.act(session, "read", 0, "", list(range(1, 11)))).startswith("NOT RUN: read takes at most 8")
    assert asyncio.run(highways.act(session, "read", 0, "launch room")).startswith(
        'NOT RUN: App data already shows "Launch room" above the chat list; open_chat one to go further back')
    # From an open chat, read leaves it for the list, where it shows; list does the same.
    in_chat = phone(chat_screen("Ana Lima", "Lunch on Friday works for me"))
    line = asyncio.run(highways.act(in_chat, "read", 0, "launch room"))
    assert line.startswith('went back from the conversation; App data now shows "Launch room"')
    assert line.endswith("open the Chat tab")
    assert asyncio.run(highways.act(in_chat, "list")).startswith("went back from the conversation")
    assert in_chat.opened == [("key", "back"), ("key", "back")]


def test_older_in_a_chat_names_the_chat(phone, monkeypatch):
    monkeypatch.setattr(teams, "MESSAGES_SHOWN", 2)
    session = phone(chat_screen("Ana, Ben, and Cai", "Shipping the release candidate tonight"))
    asyncio.run(highways.block(session))
    assert asyncio.run(highways.act(session, "older")) == (
        'App data now shows the 2 messages of "Ana, Ben, and Cai" before the ones it showed (from the phone)')


def test_an_open_chat_teams_checked_after_its_last_message_is_synced(tmp_path):
    # Some chats never get a counter that meets: on screen, Teams' own check time says it has synced them.
    build(tmp_path / "main.db", synced_group=False)
    db = sqlite3.connect(tmp_path / "main.db")
    db.execute("update ChatConversation set lastMessageSequenceId=30 where conversationId=?", (GROUP,))
    db.execute("insert into MessageSyncState values (?, ?)", (GROUP, NOW))
    db.commit()
    db.close()
    in_chat = Session(tmp_path, chat_screen("Ana, Ben, and Cai", "Shipping the release candidate tonight"))
    text = asyncio.run(highways.block(in_chat))
    assert ("Synced to the newest. The phone has 4 of about 30 messages and events, back to" in text
            and "app_action older loads earlier ones from Teams." in text)
    assert asyncio.run(highways.act(in_chat, "open_chat", 0, "Ana, Ben, and Cai")) == (
        '"Ana, Ben, and Cai" is already open: App data shows it')


def test_older_that_reached_the_top_is_not_scrolled_again(tmp_path, monkeypatch):
    # Teams' counter also counts items it never stores: after one scroll to the top that loaded nothing, App data
    # says that is all, and asking again costs nothing.
    monkeypatch.setattr(teams, "FLINGS_PER_CHECK", 15)
    build(tmp_path / "main.db", synced_group=False)
    db = sqlite3.connect(tmp_path / "main.db")
    db.execute("update ChatConversation set lastMessageSequenceId=30 where conversationId=?", (GROUP,))
    db.execute("insert into MessageSyncState values (?, ?)", (GROUP, NOW))
    db.commit()
    db.close()
    session = Session(tmp_path, chat_screen("Ana, Ben, and Cai", "Shipping the release candidate tonight"))
    assert "app_action older loads earlier ones from Teams." in asyncio.run(highways.block(session))
    assert asyncio.run(highways.act(session, "older")).startswith("no older messages came after scrolling to the top")
    swipes = session.swipes
    assert swipes > 0
    text = asyncio.run(highways.block(session))
    assert "That is all Teams gives of this chat: 4 messages and events" in text and "older loads" not in text
    assert asyncio.run(highways.act(session, "older")).startswith("nothing older: older already scrolled")
    assert session.swipes == swipes


def test_open_chat_is_synced_once_teams_has_checked_it(tmp_path, monkeypatch):
    build(tmp_path / "main.db", synced_group=False)
    session = Session(tmp_path, list_screen("Ana Lima"))
    opened = session.run

    async def run(op):
        # Teams opens the chat and checks it with its server: the counter stays behind, the check time moves.
        db = sqlite3.connect(tmp_path / "main.db")
        db.execute("insert into MessageSyncState values (?, ?)", (GROUP, NOW + 5))
        db.commit()
        db.close()
        return await opened(op)

    session.lane.run = run
    line = asyncio.run(highways.act(session, "open_chat", 0, "Ana, Ben, and Cai"))
    assert line.startswith('opened "Ana, Ben, and Cai" (group); Teams synced its newest messages in ')
