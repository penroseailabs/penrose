"""Microsoft Teams' Highway: chats and channels read from Teams' own database (SkypeTeams_<user id>.db) instead of
the screen.

What it covers, and what it guarantees (measured on Teams 1416/1.0.0.2026193004, 2026-11-07):
- The chat list (Chat tab): chats and followed channels newest first, each with its unread count and last message
  whole, and a number to open it by; every unread one is listed, however far down. Unread counts what Teams counts:
  messages after the read mark (consumptionHorizon) from someone else, not calls or membership events. Checked
  against the chat names on screen before it is shown.
- A chat or channel on screen: its messages, oldest first, with exact times, whole texts, who wrote, what they reply
  to (a channel's posts with their subject, replies marked), files, images, reactions, edits and deletions. Teams
  keeps a conversation's history only as far as it has synced it: `lastMessageSequenceId` is the server's message
  counter and `lastMessageSequenceIdAtSync` how far the phone has synced, and `MessageSyncState.lastSyncCheckTime` is
  when Teams last checked the conversation with its server (some conversations never get a counter: opened and
  synced, they still read 0 or behind). So it holds the newest messages when the counters meet or when that check is
  later than its last message, and the block says so and how much the phone has. Which conversation it is, is decided by its title and by the
  messages on screen, and the block is checked against them.
- read: on the chat list, the messages of chats or channels, shown above the list in one step. Teams loads a
  conversation's messages only when it is opened: one never opened on this phone holds just its last message, one
  opened long ago a page of 50 and whatever was pushed since (measured 2026-11-12: 137 of 274 chats held one message;
  no counter or sync time in the database proves a conversation was loaded, only the count of its messages against
  the server's counter). So read shows a conversation from the database without opening it only when the phone
  holds it whole (as many messages as the counter; it then stays unread); any other it opens by its link, waits for
  Teams to load it, and goes back to the list. Each one says what is shown: the whole conversation, or its newest N
  of about M. They stay shown while others are read (the READ_CHATS read last).
- list: the chat list from its start again (from a chat: Back to it first).
- open_chat: opens a chat or channel by its link on Teams' own interface (from the list: a link sent while another
  conversation is open does not navigate) and waits until Teams has synced its newest messages (1-12 s measured).
- older: shows earlier messages the phone already has; past those, scrolls the conversation up on the interface until
  Teams has loaded its next page (about 50 messages; 5-20 s, longer the more is loaded) and shows that.
- Search is not covered: it runs on Microsoft's servers and finds what the phone has not synced. A result opens the
  conversation at the message found; the block then shows what Teams saved around it.
"""

import asyncio
import html
import json
import re
import time
from datetime import datetime
from urllib.parse import quote

from . import Highway, HighwayError, clip

PACKAGE = "com.microsoft.teams"
LINK = "https://teams.microsoft.com/l/chat/{}/conversations"
CHANNEL_LINK = "https://teams.microsoft.com/l/channel/{}/{}"
CHATS_SHOWN = 30  # conversations listed on the chat list (and every unread one besides)
CHATS_SCANNED = 2000  # chats read: all of them, to list, to match the screen and to open by name
CHATS_PAGE = 50  # chats on each later page of the chat list (app_action older)
CHATS_KEPT = 80  # chats of the pages before it that a later page still lists, as short rows
MESSAGES_SHOWN = 40
READ_CHATS = 8  # conversations app_action read keeps shown above the chat list, without opening them
READ_MESSAGES = 96  # messages shared between them (one alone gets MESSAGES_SHOWN)
READ_ROOM = 10000  # characters shared between them
READ_CHARS = 320  # of one message when several conversations are read at once
UNREAD_SHOWN = 5  # unread messages listed under each unread conversation on the chat list
AROUND = 12  # messages shown before and after the ones on screen, for a conversation Teams has not synced
TEXT_CHARS = 700  # of one message
BLOCK_CHARS = 14000
SYNC_WAIT = 20  # seconds open_chat waits for Teams to sync the conversation
SYNC_SETTLE = 4  # seconds a conversation whose counters already meet gets to show that Teams is loading it
LOADED_FOR = 600  # seconds a conversation opened in this session counts as loaded
LOAD_PAGE = 50  # messages Teams loads when a conversation is opened: fewer on the phone after that is all of them
OLDER_SECONDS = 45  # scrolling up for the next page of history gives up after this
FLINGS_PER_CHECK = 5
CHAT_SCREEN, CHANNEL_SCREEN = "ChatsActivity", "ConversationsActivity"

REQUIRED = {
    "ChatConversation": {"conversationId", "topic", "consumptionHorizon", "lastMessageArrivalTime",
                         "lastMessageSequenceId", "lastMessageSequenceIdAtSync", "totalMemberCount", "isDeleted",
                         "leftConversation"},
    "Conversation": {"conversationId", "displayName", "parentConversationId", "isFavorite", "consumptionHorizon",
                     "lastMessageArrivalTime", "lastMessageSequenceId", "lastMessageSequenceIdAtSync", "isDeleted",
                     "leftConversation"},
    "Message": {"conversationId", "messageId", "arrivalTime", "from", "userDisplayName", "content", "messageType",
                "deleteTime", "editTime", "emotionCount", "mentionsMe", "parentMessageId", "subject"},
    "ThreadUser": {"threadId", "userId"},
    "User": {"mri", "displayName", "givenName"},
    "ActivityFeed": {"activityType", "activitySubtype", "activityTimestamp", "isRead", "sourceThreadId",
                     "sourceThreadTopic", "sourceUserImDisplayName", "messagePreview", "isDismissed"},
    "MessagePropertyAttribute": {"messageId", "conversationId", "attributeName", "attributeValue"},
    "MessageSyncState": {"conversationId", "lastSyncCheckTime"},
}
LISTED = ("coalesce(isDeleted,0)=0 and coalesce(leftConversation,0)=0 and coalesce(lastMessageArrivalTime,0)>0 "
          "and conversationId like '19:%'")
MESSAGE_COLUMNS = ('messageId, arrivalTime, "from" as sender, userDisplayName, content, messageType, deleteTime, '
                   "editTime, emotionCount, mentionsMe, parentMessageId, subject")
LAST_COLUMNS = ("l.messageId, l.arrivalTime, l.sender, l.userDisplayName, l.content, l.messageType, l.deleteTime, "
                "l.editTime, l.emotionCount, l.mentionsMe, l.parentMessageId, l.subject")
# The read mark: the first part of consumptionHorizon. Unread: a message after it, from someone else, that is a
# message (calls, membership events and deleted messages do not make a conversation unread).
HORIZON = "cast(substr(c.consumptionHorizon,1,instr(c.consumptionHorizon||';',';')-1) as integer)"
UNREAD = (f"(select count(*) from Message m where m.conversationId=c.conversationId and m.arrivalTime > {HORIZON} "
          "and m.messageType not like 'ThreadActivity/%' and m.messageType != 'Event/Call' "
          "and coalesce(m.deleteTime,0)=0 and m.\"from\" != ?)")
LATEST = ("l as (select m.conversationId, " + MESSAGE_COLUMNS.replace("messageId,", "m.messageId,", 1)
          + ", row_number() over (partition by m.conversationId order by m.arrivalTime desc) as rn from Message m "
          "where m.conversationId in (select conversationId from c) and m.messageType not like 'ThreadActivity/%')")
# A conversation's sync counters (a chat's or a channel's): the server's message counter, how far the phone synced,
# how many it has, the oldest it has.
SYNC_SQL = """with t as (select lastMessageSequenceId as seq, lastMessageSequenceIdAtSync as synced from ChatConversation
                         where conversationId=?
                         union all select lastMessageSequenceId, lastMessageSequenceIdAtSync from Conversation
                         where conversationId=?)
              select t.seq, t.synced, (select count(*) from Message where conversationId=?) as stored,
                     (select min(arrivalTime) from Message where conversationId=?) as oldest,
                     (select max(lastSyncCheckTime) from MessageSyncState where conversationId=?) as checked
              from t limit 1"""
CHECKED = "(select max(lastSyncCheckTime) from MessageSyncState s where s.conversationId=c.conversationId) as checked"


ACTIVITY_SHOWN = 40
ACTIVITY_SQL = """select activityType, activitySubtype, activityTimestamp, isRead, sourceThreadId, sourceThreadTopic,
                         sourceUserImDisplayName, messagePreview from ActivityFeed where coalesce(isDismissed,0)=0
                  order by activityTimestamp desc limit ?"""


def sync_params(conv):
    return [conv, conv, conv, conv, conv]


def number(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def current(seq, synced, checked, last):
    """Whether the phone holds a conversation's newest messages: Teams synced it to the server's counter, or
    checked it with the server after its last message arrived."""
    return (number(seq) > 0 and number(synced) >= number(seq)) or (number(checked) > 0 and number(checked) >= number(last))


class Teams(Highway):
    package = PACKAGE
    name = "Microsoft Teams"
    file = r"SkypeTeams_[0-9a-f-]{36}\.db"
    # The agent's whole guide to Teams, shown with App data while Teams is in front and nowhere else.
    guide = ("read (items=[numbers in the list below], or text=\"a chat's name\") shows chats' or channels' messages "
             "right here, above the list, in one step: one the phone already holds whole is read from its data "
             "without opening it (it stays unread); any other is opened so that Teams loads it (Teams loads a chat "
             "only when it is opened; that marks it read), and then the list is back. Each says what is shown of it, "
             "the whole chat or its newest N of about M, and what you read stays shown while you read others (the 8 "
             "read last). To learn what chats say, read them this way, several at once, also when your brief says to "
             "open them one by one; never judge a chat by its last message in the list. open_chat (item=N or "
             "text=\"name\") opens one from anywhere and waits until Teams has loaded its newest messages: use it to "
             "do something in a chat, or to go further back in it with older. older shows the next older chats on the "
             "chat list (it is newest first: stop where the dates pass what you need), and in an open chat or channel "
             "its earlier messages (the phone's first, then Teams loads more). list shows the chat list from its "
             "start again (from an open chat it goes back to the list, at the page you left). Open, read and page "
             "chats only this way, never through Teams' own list, its scrolling or a tap on a row: App data already "
             "lists every chat on the phone, more than Teams' list shows. A number is a chat's place in the list now "
             "and moves when new messages come: take it from the list in front of you. Teams' search stays Teams' "
             "own: use it as usual, it searches Teams' servers.")
    anywhere = ("open_chat",)  # usable from outside Teams: the link opens it

    def __init__(self, session):
        super().__init__(session)
        self.checked = None  # None: the database not checked yet; "": fine; else why it cannot be read
        self.listed = []  # the conversation ids in the order the last chat list numbered them (open_chat item=N)
        self.chat = None  # the conversation on screen at the last look
        self.view = None  # None: the newest messages; ("before", arrival ms): the page before that message
        self.first_shown = None  # arrival ms of the oldest message the last block showed
        self.opened = None  # (conversation id, when) of the conversation open_chat opened
        self.list_page = 0  # where the chat list's page starts (older moves it on)
        self.list_end = 0  # the last number the chat list showed
        self.on_list = False  # the last look was at the chat list
        self.reading = []  # conversation ids app_action read asked for, shown above the chat list
        self.loaded = {}  # conversation id -> when it was opened in this session, so Teams loaded it
        self.chat_title = ""
        self.topped = {}  # conversation id -> messages on the phone when older scrolled to its top and no more came

    @property
    def me(self):
        """The signed-in user's Teams id, from the database's name."""
        match = re.search(r"SkypeTeams_([0-9a-f-]{36})\.db", self.file_name or "")
        return f"8:orgid:{match.group(1)}" if match else "8:orgid:?"

    # --- reading ------------------------------------------------------------------------------------------

    async def check(self):
        """Whether Teams' database still has what this Highway reads (a Teams update may change it)."""
        if self.checked is None:
            rows = (await self.query(schema=(
                "select m.name as tbl, p.name as col from sqlite_master m join pragma_table_info(m.name) p "
                "where m.type='table' and m.name in (" + ",".join("?" * len(REQUIRED)) + ")", list(REQUIRED))))["schema"]
            have = {}
            for row in rows:
                have.setdefault(row["tbl"], set()).add(row["col"])
            missing = [f"{t}.{c}" for t, cols in REQUIRED.items() for c in sorted(cols - have.get(t, set()))]
            self.checked = f"Teams' data has changed shape ({', '.join(missing[:4])} missing)" if missing else ""
        if self.checked:
            raise HighwayError(self.checked)

    def list_queries(self):
        """The queries for every chat and channel, read back by conversations()."""
        me = self.me
        return {
            "chats": (f"""with c as (select conversationId, topic, consumptionHorizon, lastMessageArrivalTime,
                         lastMessageSequenceId, lastMessageSequenceIdAtSync, totalMemberCount from ChatConversation
                         where {LISTED} order by lastMessageArrivalTime desc limit ?), {LATEST}
                       select c.*, {UNREAD} as unread_n, {CHECKED},
                              (select count(*) from ThreadUser t where t.threadId=c.conversationId) as members,
                              {LAST_COLUMNS}
                       from c left join l on l.conversationId=c.conversationId and l.rn=1
                       order by c.lastMessageArrivalTime desc""", [CHATS_SCANNED, me]),
            "names": (f"""select threadId, userId, displayName, givenName, others from (
                         select t.threadId, t.userId, u.displayName, u.givenName,
                                row_number() over (partition by t.threadId
                                                   order by lower(coalesce(u.givenName, u.displayName, ''))) as rn,
                                count(*) over (partition by t.threadId) as others
                         from ThreadUser t left join User u on u.mri=t.userId
                         where t.userId != ? and t.threadId in (
                             select conversationId from ChatConversation where {LISTED} and coalesce(topic,'')=''
                             order by lastMessageArrivalTime desc limit ?))
                       where rn <= 3""", [me, CHATS_SCANNED]),
            "channels": (f"""with c as (select c.conversationId, c.displayName, c.parentConversationId,
                         coalesce(p.displayName, '') as team, c.isFavorite, c.consumptionHorizon,
                         c.lastMessageArrivalTime, c.lastMessageSequenceId, c.lastMessageSequenceIdAtSync
                         from Conversation c left join Conversation p on p.conversationId=c.parentConversationId
                         where coalesce(c.isDeleted,0)=0 and coalesce(c.leftConversation,0)=0
                         and c.conversationId like '19:%@thread.tacv2'), {LATEST}
                       select c.*, {UNREAD} as unread_n, {CHECKED}, {LAST_COLUMNS}
                       from c left join l on l.conversationId=c.conversationId and l.rn=1""", [me]),
            "total": (f"select count(*) as n from ChatConversation where {LISTED}", []),
        }

    async def conversations(self, **more):
        """(chats and channels: id, title, kind, people, unread, last message, sync counters, link; how many chats
        there are; every row read, with those of `more`, queries asked in the same read)."""
        got = await self.query(**self.list_queries(), **more)
        members = {}
        for row in got["names"]:
            members.setdefault(row["threadId"], []).append(row)
        out = []
        for row in got["chats"]:
            conv = row["conversationId"]
            out.append({**row, "id": conv, "title": title(row.get("topic"), members.get(conv, [])),
                        "kind": kind(conv), "people": row.get("totalMemberCount") or row.get("members"),
                        # A group nobody named: Teams shows its people's names as its title.
                        "unnamed": kind(conv) == "group" and not row.get("topic"), "link": LINK.format(conv),
                        "names": {norm(title(row.get("topic"), members.get(conv, [])))}})
        for row in got["channels"]:
            conv, root = row["conversationId"], not row.get("parentConversationId")
            name = "General" if root else row.get("displayName") or "?"
            team = row.get("displayName") if root else row.get("team")
            followed = bool(row.get("isFavorite"))
            out.append({**row, "id": conv, "title": name, "kind": f"channel in team {team}", "people": None,
                        "channel": True, "followed": followed, "unread_n": row["unread_n"] if followed else 0,
                        "link": CHANNEL_LINK.format(quote(conv, safe=""), quote(name)),
                        "names": {norm(row.get("displayName")), norm(f"{team} {name}"), norm(name) if not root else ""}})
        out.sort(key=lambda c: c.get("lastMessageArrivalTime") or 0, reverse=True)
        return out, (got["total"][0]["n"] if got["total"] else len(out)), got

    @staticmethod
    def front(screen):
        """Which of the screens this Highway covers is in front: "chat", "channel", "list", "activity" or None."""
        if not screen or screen.header.get("app") != PACKAGE:
            return None
        activity = str(screen.header.get("activity") or "")
        fragments = " ".join(str(f) for f in screen.header.get("fragments") or [])
        if activity.endswith(CHAT_SCREEN) and "ChatFragment" in fragments:
            return "chat"
        if activity.endswith(CHANNEL_SCREEN) and "ConversationsWithComposeFragment" in fragments:
            return "channel"
        if activity.endswith("MainActivity") and "ConversationListFragment" in fragments:
            return "list"
        if activity.endswith("MainActivity") and "ActivityListFragment" in fragments:
            return "activity"
        return None

    async def block(self, screen):
        front = self.front(screen)
        if front in ("chat", "channel"):
            await self.check()
            return await self.conversation_block(screen, channel=front == "channel")
        self.chat, self.view = None, None
        if front == "list":
            await self.check()
            self.on_list = True
            return await self.list_block(screen)
        self.on_list, self.list_page, self.reading = False, 0, []
        if front == "activity":
            await self.check()
            return await self.activity_block()
        return None

    async def activity_block(self):
        """The Activity tab: mentions, replies and reactions, newest first, with the chat or channel each is in."""
        everything, _, got = await self.conversations(activity=(ACTIVITY_SQL, [ACTIVITY_SHOWN]))
        return "\n".join(["Activity, newest first: when | what | who | where | preview", *self.activity_lines(everything, got)])

    def activity_lines(self, everything, got):
        titles_by_id = {c["id"]: f'{c["title"]} ({c["kind"]})' if c.get("channel") else c["title"] for c in everything}
        lines = []
        for a in got.get("activity") or []:
            what = f"{a['activityType']} {a['activitySubtype'] or ''}".strip()
            lines.append(f"- {self.when(a['activityTimestamp'])} | {what}{'' if a['isRead'] else ' (UNREAD)'} | "
                         f"{a['sourceUserImDisplayName'] or '?'} | "
                         f"{a['sourceThreadTopic'] or titles_by_id.get(a['sourceThreadId']) or 'a chat'} | "
                         f"{clip(a['messagePreview'], 160)}")
        return lines

    async def list_block(self, screen):
        everything, _, got = await self.conversations(activity=(ACTIVITY_SQL, [12]))
        shown_names = texts_by_id(screen, "chat_display_name")
        known = set().union(*(c["names"] for c in everything)) if everything else set()
        matched = [n for n in shown_names if norm(n) in known]
        if len(shown_names) >= 2 and len(matched) * 2 < len(shown_names):
            raise HighwayError(f"the chats on screen do not match Teams' data ({len(matched)} of {len(shown_names)} found)")
        listed = [c for c in everything if not c.get("channel") or c.get("followed")]
        # Numbered by place in the whole list, the same on every page, so open_chat item=N always means one chat.
        self.listed = [c["id"] for c in listed]
        reading = [c for conv in self.reading for c in everything if c["id"] == conv]
        # While chats are read above it, the list keeps its place and shows short rows (it was read in full before).
        read = await self.read_lines(reading) if reading else []
        first = self.list_page == 0
        size = CHATS_SHOWN if first else CHATS_PAGE
        self.list_page = min(self.list_page, max(0, len(listed) - 1))
        numbered = list(enumerate(listed, 1))
        page = numbered[self.list_page:self.list_page + size]
        end = page[-1][0] if page else 0
        # The pages before this one stay listed as short rows, so nothing seen has to be paged back to.
        before = numbered[max(0, self.list_page - CHATS_KEPT):self.list_page]
        # Every unread one is on the first page, wherever it is in the list.
        further = [(n, c) for n, c in enumerate(listed, 1) if c["unread_n"] and n > end] if first and not read else []
        unread = [c for c in listed if c["unread_n"]]
        lines = [f"Chats and followed channels, newest first: {page[0][0] if page else 0}-{end} of {len(listed)}, all "
                 "that are on the phone (Teams' own list on screen stops at the recent ones)"
                 + ("" if end >= len(listed) else f"; app_action older shows the next {min(CHATS_PAGE, len(listed) - end)}"
                    f", all last active before {self.when(page[-1][1].get('lastMessageArrivalTime'))}")
                 + ("" if first else "; app_action list shows the list from its start again")
                 + (f"; {before[0][0]}-{before[-1][0]}, shown before, are listed first as short rows (name, kind, "
                    "last active)" if before else "")
                 + f". {len(unread)} unread in all{', every one listed here' if first and not read else ''}. "
                 + ("N. \"name\" (kind, people, unread) | last active (short rows while chats are read above)" if read
                    else "N. \"name\" (kind, people, unread) | last message: when, who: text")]
        kept = [f"{n}. {self.list_line(c, short=True)}" for n, c in before]
        lines += kept
        news = {} if read else await self.unread_messages([c for _, c in page + further if c["unread_n"]])
        for i, (n, c) in enumerate(page + further):
            if i == len(page):
                lines.append("Unread further down the list:")
            lines.append(f"{n}. {self.list_line(c, short=bool(read))}")
            if c["id"] in news:
                # Read here, the chat stays unread for the user: opening it would mark it read.
                rows = news[c["id"]]
                lines.append(f"   its unread messages, oldest first ({len(rows)} of {c['unread_n']}; read here, "
                             "they stay unread in Teams):")
                lines += [f"   - {self.when(r['arrivalTime'])} | {self.message_text(r, {}, {})}" for r in rows]
        if end >= len(listed):
            lines.append("(the end of the list: that is every chat and followed channel on the phone)")
        if differ := unread_differs(screen, everything):
            lines.append("Teams' own rows on screen show unread differently for: " + "; ".join(differ)
                         + ". For those, go by Screen.")
        if got.get("activity") and first and not read:
            lines.append("Activity (mentions, replies, reactions), newest first: when | what | who | where | preview")
            lines += self.activity_lines(everything, got)
        self.list_end = end
        return fit("\n".join(read + lines), limit=BLOCK_CHARS + (READ_ROOM if read else 0)
                   + sum(len(line) + 1 for line in kept))

    async def unread_messages(self, unread):
        """{conversation id: its newest UNREAD_SHOWN unread messages, oldest first} (what makes it unread)."""
        if not unread:
            return {}
        rows = (await self.query(news=(f"""select * from (select m.conversationId, {MESSAGE_COLUMNS.replace('messageId,', 'm.messageId,', 1)},
                    row_number() over (partition by m.conversationId order by m.arrivalTime desc) as rn
                from Message m join (select conversationId, consumptionHorizon from ChatConversation
                                     union all select conversationId, consumptionHorizon from Conversation) c
                  on c.conversationId=m.conversationId
                where m.conversationId in ({",".join("?" * len(unread))}) and m.arrivalTime > {HORIZON}
                  and m.messageType not like 'ThreadActivity/%' and m.messageType != 'Event/Call'
                  and coalesce(m.deleteTime,0)=0 and m."from" != ?) where rn <= ? order by arrivalTime""",
                [*(c["id"] for c in unread), self.me, UNREAD_SHOWN])))["news"]
        found = {}
        for row in rows:
            found.setdefault(row["conversationId"], []).append(row)
        return found

    async def read_lines(self, chats):
        """Above the chat list: the messages of the conversations app_action read asked for, from the phone's data,
        each with what the phone holds of it. Nothing is opened, so nothing is marked read."""
        ids = [c["id"] for c in chats]
        marks = ",".join("?" * len(ids))
        each = MESSAGES_SHOWN if len(ids) == 1 else max(8, READ_MESSAGES // len(ids))
        got = await self.query(
            held=("select conversationId, count(*) as stored, min(arrivalTime) as oldest, max(arrivalTime) as newest "
                  f"from Message where conversationId in ({marks}) group by conversationId", ids),
            rows=(f"""select * from (select m.conversationId, {MESSAGE_COLUMNS.replace('messageId,', 'm.messageId,', 1)},
                        row_number() over (partition by m.conversationId order by m.arrivalTime desc) as rn
                      from Message m where m.conversationId in ({marks})) where rn <= ? order by arrivalTime desc""",
                  [*ids, each]),
            members=(f"""select t.threadId, t.userId, u.displayName from ThreadUser t left join User u on u.mri=t.userId
                         where t.threadId in ({marks}) limit 2000""", ids),
            senders=(f"""select mri, displayName from User where mri in
                         (select distinct "from" from Message where conversationId in ({marks}))""", ids),
            files=(f"""select messageId, attributeValue from MessagePropertyAttribute
                       where conversationId in ({marks}) and attributeName='fileName'""", ids),
        )
        held = {h["conversationId"]: h for h in got["held"]}
        names = {m["mri"]: m["displayName"] for m in got["senders"] if m.get("displayName")}
        people = {}
        for m in got["members"]:
            if m.get("displayName"):
                names[m["userId"]] = m["displayName"]
                if m["userId"] != self.me:
                    people.setdefault(m["threadId"], []).append(m["displayName"])
        files = {}
        for f in got["files"]:
            files.setdefault(str(f["messageId"]), []).append(f["attributeValue"])
        rows = {}
        for row in got["rows"]:  # newest first
            rows.setdefault(row["conversationId"], []).append(row)
        several = len(chats) > 1
        lines = [f"Read for you: {len(chats)} conversation{'s' if several else ''}. One the phone held whole was read "
                 "from its data without opening it (it stays unread); any other was opened so that Teams loaded it. "
                 "Each says what is shown of it. They stay shown here while you read others (the "
                 f"{READ_CHATS} read last). when | who: text"]
        room = READ_ROOM // len(chats)
        for c in chats:
            conv, h = c["id"], held.get(c["id"]) or {}
            what = "channel" if c.get("channel") else "chat"
            who = people.get(conv, [])
            lines.append(f'{self.listed.index(conv) + 1}. "{c["title"]}" ({", ".join(sort_of(c))}'
                         + (f": {', '.join(who[:8])}" + (f" and {len(who) - 8} more" if len(who) > 8 else "") + ", you"
                            if who and c["kind"] != "1:1" else "") + "). " + self.held(c, h, what))
            shown, used = [], 0
            for row in rows.get(conv, []):
                line = (f"   - {self.when(row['arrivalTime'])} | "
                        f"{self.message_text(row, names, files, chars=READ_CHARS if several else TEXT_CHARS)}")
                if shown and used + len(line) + 1 > room:
                    break
                shown.append(line)
                used += len(line) + 1
            earlier = (h.get("stored") or 0) - len(shown)
            if shown:
                lines.append(f"   Newest {len(shown)} of its messages and events on the phone, oldest first"
                             + (f"; {earlier} earlier ones on the phone are not shown (open_chat it to read them)"
                                if earlier > 0 else "") + ":")
                lines += reversed(shown)
        return [*lines, ""]

    def fresh(self, conv):
        """Whether the conversation was opened in this session, so Teams loaded its newest messages."""
        return conv in self.loaded and time.monotonic() - self.loaded[conv] < LOADED_FOR

    @staticmethod
    def whole(c, stored):
        """Whether the phone holds every message of a conversation: as many as the server's counter. Nothing else
        in Teams' data proves it (its sync counters and times also move without a message being loaded)."""
        seq = number(c.get("lastMessageSequenceId"))
        return seq > 0 and number(stored) >= seq

    def held(self, c, h, what="chat"):
        """What the phone holds of a conversation read above the list, in one sentence that never says more than
        the counts prove."""
        seq, last = number(c.get("lastMessageSequenceId")), number(c.get("lastMessageArrivalTime"))
        stored = h.get("stored") or 0
        if not stored:
            return (f"Teams has no messages of this {what} on the phone" + (", also after opening it."
                    if self.fresh(c["id"]) else ": open_chat loads them."))
        if number(h.get("newest")) < last:
            return (f"The phone does NOT have this {what}'s newest messages (it has {stored}, the last from "
                    f"{self.when(h['newest'])}): open_chat loads them.")
        if self.whole(c, stored):
            return f"The phone has the whole {what} ({stored} messages and events)."
        if self.fresh(c["id"]):
            if stored < LOAD_PAGE or seq <= stored:
                # Less than the page Teams loads at once: there is nothing older to load.
                return (f"Opened just now so that Teams loaded it: that is all Teams gives of this {what}, {stored} "
                        "messages and events" + (f" (its counter says about {seq}: it also counts ones it does not "
                                                 "keep)" if seq > stored else "") + ".")
            return (f"Opened just now so that Teams loaded it: the phone has its newest {stored} messages and events "
                    f"of about {seq}; the older ones are NOT shown and load with open_chat, then older.")
        return (f"NOT loaded: Teams loads a {what} only when it is opened, and the phone has just {stored}"
                f"{f' of about {seq}' if seq else ''} messages and events of this one, back to "
                f"{self.when(h.get('oldest'))}. What is shown is NOT the conversation: open_chat loads it.")

    def list_line(self, c, short=False):
        tags = sort_of(c)
        if c["unread_n"]:
            tags.append(f"UNREAD: {c['unread_n']} new")
        if short:
            return f'"{c["title"]}" ({", ".join(tags)}) | {self.when(c["lastMessageArrivalTime"])}'
        last = self.message_text(c, {}, {}, short=True) if c.get("messageId") else "(no message on the phone)"
        return f'"{c["title"]}" ({", ".join(tags)}) | {self.when(c.get("arrivalTime") or c["lastMessageArrivalTime"])}, {last}'

    async def identify(self, screen, channel):
        """The conversation on screen: (its row, ids of its messages found on screen, the screen shows message text).
        Decided by its title and by the messages shown; raises when Teams' data has nothing that fits."""
        header = next((el.label for el in screen.elements.values() if el.node.get("id") == "infoLayout"), "")
        heading = norm(header.split(" · ")[0])
        snippets = anchors(screen)
        more = {"anchors": ("select conversationId, messageId from Message where "
                            + " or ".join("content like ?" for _ in snippets) + " limit 400",
                            [f"%{piece}%" for piece in snippets])} if snippets else {}
        everything, _, got = await self.conversations(**more)
        mine = [c for c in everything if bool(c.get("channel")) == channel]
        by_title = {c["id"] for c in mine if heading and heading in c["names"]}
        hits = {}
        for row in got.get("anchors", []):
            hits.setdefault(row["conversationId"], set()).add(row["messageId"])
        ids = {c["id"] for c in mine}
        hits = {conv: found for conv, found in hits.items() if conv in ids}
        opened = self.opened[0] if self.opened and time.monotonic() - self.opened[1] < 600 else None
        candidates = by_title | set(hits) | ({opened} if opened in ids else set())
        if not candidates:
            raise HighwayError(f"the {'channel' if channel else 'chat'} on screen is not in Teams' data yet")
        best = max(candidates, key=lambda c: (len(hits.get(c, ())) + 2 * (c in by_title) + (c == opened)))
        if snippets and not hits.get(best) and best not in by_title:
            raise HighwayError("the messages on screen are not in Teams' data yet")
        row = next(c for c in mine if c["id"] == best)
        return row, hits.get(best, set()), bool(snippets)

    def window(self, conv, mode, mark=None, ids=(), prefix=""):
        """The queries for the messages a block shows (read back by messages()): the newest ("newest"), the page
        before arrival time `mark` ("before"), or the messages with `ids` and AROUND on each side ("around"); "below"
        counts the messages older than the first shown."""
        pick = f"select {MESSAGE_COLUMNS} from Message where conversationId=?"
        if mode == "around":
            marks = ",".join("?" * len(ids))
            lo = f"(select min(arrivalTime) from Message where conversationId=? and messageId in ({marks}))"
            hi = f"(select max(arrivalTime) from Message where conversationId=? and messageId in ({marks}))"
            return {prefix + k: v for k, v in {
                "before": (f"{pick} and arrivalTime < {lo} order by arrivalTime desc limit ?", [conv, conv, *ids, AROUND]),
                "middle": (f"{pick} and arrivalTime between {lo} and {hi} order by arrivalTime desc",
                           [conv, conv, *ids, conv, *ids]),
                "after": (f"select * from ({pick} and arrivalTime > {hi} order by arrivalTime limit ?) "
                          "order by arrivalTime desc", [conv, conv, *ids, AROUND]),
                "below": (f"select count(*) as n from Message where conversationId=? and arrivalTime < {lo}",
                          [conv, conv, *ids]),
            }.items()}
        where, params = ("and arrivalTime < ?", [mark]) if mode == "before" else ("", [])
        return {prefix + k: v for k, v in {
            "newest": (f"{pick} {where} order by arrivalTime desc limit ?", [conv, *params, MESSAGES_SHOWN]),
            "below": ("select count(*) as n from Message where conversationId=? and arrivalTime < "
                      f"(select min(arrivalTime) from (select arrivalTime from Message where conversationId=? {where} "
                      "order by arrivalTime desc limit ?))", [conv, conv, *params, MESSAGES_SHOWN]),
        }.items()}

    @staticmethod
    def messages(got, prefix=""):
        """The rows a window's queries read, oldest first, and how many older ones the phone has besides."""
        got = {k.removeprefix(prefix): v for k, v in got.items() if k.startswith(prefix)}
        if "middle" in got:
            rows = [*got["after"], *got["middle"], *got["before"]]  # each newest first
            below = (got["below"][0]["n"] if got["below"] else 0) - len(got["before"])
        else:
            rows, below = got["newest"], (got["below"][0]["n"] if got["below"] else 0)
        return list(reversed(rows)), max(0, below or 0)

    async def conversation_block(self, screen, channel):
        chat, on_screen, shows_text = await self.identify(screen, channel)
        conv = chat["id"]
        if self.chat != conv:
            self.view = None
        self.chat, self.chat_title = conv, chat["title"]
        seq, synced = chat.get("lastMessageSequenceId") or 0, chat.get("lastMessageSequenceIdAtSync") or 0
        # Which messages: the page older's last call asked for; else the newest, or, when the conversation on screen
        # is scrolled away from them (or Teams has not synced its newest), the messages on screen and some around them.
        if self.view and self.view[0] == "before":
            window, shown_as = self.window(conv, "before", self.view[1], prefix="w_"), "earlier messages (app_action older for more)"
        else:
            window, shown_as = self.window(conv, "newest", prefix="w_"), "the newest messages"
            if on_screen:
                window.update(self.window(conv, "around", ids=sorted(on_screen), prefix="a_"))
        got = await self.query(
            sync=(SYNC_SQL, sync_params(conv)),
            members=("""select t.userId, u.displayName from ThreadUser t left join User u on u.mri=t.userId
                        where t.threadId=? limit 300""", [conv]),
            senders=("""select mri, displayName from User where mri in
                        (select distinct "from" from Message where conversationId=?)""", [conv]),
            files=("""select messageId, attributeValue from MessagePropertyAttribute
                      where conversationId=? and attributeName='fileName'""", [conv]),
            **window,
        )
        sync = got["sync"][0] if got["sync"] else {"seq": 0, "synced": 0, "stored": 0, "oldest": None}
        seq, synced, stored = sync["seq"] or 0, sync["synced"] or 0, sync["stored"] or 0
        newest = current(seq, synced, sync.get("checked"), chat.get("lastMessageArrivalTime"))  # as of this read
        whole = newest and stored >= seq > 0
        members = {m["userId"]: m["displayName"] for m in got["members"] if m.get("displayName")}
        names = {**{m["mri"]: m["displayName"] for m in got["senders"] if m.get("displayName")}, **members}
        files = {}
        for f in got["files"]:
            files.setdefault(str(f["messageId"]), []).append(f["attributeValue"])
        rows, earlier = self.messages(got, "w_")
        if "a_middle" in got and not (newest and on_screen & {r["messageId"] for r in rows}):
            rows, earlier = self.messages(got, "a_")
            shown_as = "the messages on screen, with some before and after"
        self.first_shown = rows[0]["arrivalTime"] if rows else None
        what = "channel" if channel else "chat"
        if channel:
            lines = [f'Channel on screen: "{chat["title"]}" ({chat["kind"]}). Posts start with [their subject]; '
                     "replies say which post they answer."]
        else:
            people = [n for m, n in members.items() if m != self.me]
            lines = [f'Chat on screen: "{chat["title"]}" ({chat["kind"]}{", no name set" if chat.get("unnamed") else ""}'
                     + (f", {len(members) or chat.get('people') or '?'} people: {', '.join(people[:8])}"
                        + (f" and {len(people) - 8} more" if len(people) > 8 else "") + ", you" if people else "") + ")."]
        oldest = self.when(sync["oldest"]) if sync.get("oldest") else "?"
        if whole:
            lines.append(f"The phone has the whole {what} ({stored} messages and events), synced to the newest.")
        elif newest and self.topped.get(conv) == stored:
            lines.append(f"Synced to the newest. That is all Teams gives of this {what}: {stored} messages and events, "
                         f"back to {oldest}. older already scrolled it to the top and no more came"
                         + (f" (Teams' counter, about {seq}, also counts items it does not keep)." if seq > stored else "."))
        elif newest:
            lines.append(f"Synced to the newest. The phone has {stored}{f' of about {seq}' if seq else ''} messages and "
                         f"events, back to {oldest}; app_action older loads earlier ones from Teams"
                         + ("." if seq > stored else ", if it has any."))
        else:
            lines.append(f"NOT synced: Teams has not fetched this {what}'s newest messages yet (it has {stored} of about "
                         f"{seq}). app_action open_chat text=\"{chat['title']}\" opens it and waits for them.")
        if shows_text and not on_screen and newest:
            lines.append("(The messages on screen were not found in Teams' data: check Screen before relying on this.)")
        lines.append(f"Shown: {shown_as}, oldest first" + (f"; {earlier} earlier ones on the phone are not shown"
                                                            if earlier else "") + ". when | who: text")
        posts = {r["messageId"]: r for r in rows}
        missing = sorted({r["parentMessageId"] for r in rows if r.get("parentMessageId")} - set(posts)) if channel else []
        if missing:
            marks = ",".join("?" * len(missing))
            for post in (await self.query(posts=(
                    f"select messageId, subject, content from Message where conversationId=? and messageId in ({marks})",
                    [conv, *missing])))["posts"]:
                posts.setdefault(post["messageId"], post)
        for row in rows:
            lines.append(f"- {self.when(row['arrivalTime'])} | {self.message_text(row, names, files, posts=posts if channel else None)}")
        if not rows:
            lines.append("(no messages on the phone)")
        return fit("\n".join(lines), keep_end=True)

    async def sync_state(self, conv):
        rows = (await self.query(sync=(SYNC_SQL, sync_params(conv))))["sync"]
        return rows[0] if rows else None

    # --- acting -------------------------------------------------------------------------------------------

    async def act(self, action, item, text, items=()):
        if action == "open_chat":
            return await self.journal(f"app_action(open_chat, {item or text})", lambda: self.open_chat(item, text))
        if action == "read":
            numbers = list(dict.fromkeys([*(n for n in items if n), *([item] if item else [])]))
            return await self.journal(f"app_action(read, {', '.join(map(str, numbers)) or text})",
                                      lambda: self.read(numbers, text))
        if action == "older":
            return await self.journal("app_action(older)", self.older)
        if action == "list":
            return await self.journal("app_action(list)", self.back_to_list)
        return f"NOT RUN: Teams' actions are read, open_chat, older and list, not {action!r}"

    async def journal(self, command, work):
        async def run():
            line = await work()
            return not line.startswith(("FAILED", "NOT")), line, {}

        _, line = await self.s.device_action("app_action", command, run)
        return line

    def pick(self, everything, item, text):
        """(the conversation meant by a number of the list App data showed, or by its name; else why not)."""
        chat = None
        if item:
            if not self.listed or item > len(self.listed):
                return None, f"NOT RUN: there is no item {item} in the list App data showed; use text=\"its name\""
            chat = next((c for c in everything if c["id"] == self.listed[item - 1]), None)
        elif text:
            wanted = norm(text)
            exact = [c for c in everything if wanted in c["names"] or norm(c["title"]) == wanted]
            partial = exact or [c for c in everything if any(wanted in n for n in c["names"] if n)]
            if len(partial) > 1:
                return None, ("NOT RUN: several match " + repr(text) + ": "
                              + "; ".join(f'"{c["title"]}" ({c["kind"]}, {self.when(c["lastMessageArrivalTime"])})'
                                          for c in partial[:6])
                              + ". Use the full name, or open the chat list and use item=N")
            chat = partial[0] if partial else None
        if chat is None:
            return None, "NOT RUN: no Teams chat or channel matches; name it as the chat list does, or use item=N from App data"
        return chat, ""

    async def read(self, numbers, text):
        """Show conversations' messages above the chat list. One the phone holds whole is read from its data and
        nothing is done on the phone; any other is opened by its link so that Teams loads it, and left again."""
        await self.check()
        front = self.front(self.s.driver.screen)
        if front not in ("list", "chat", "channel"):
            return "NOT RUN: read works in Teams, on its chat list: open Teams' Chat tab first"
        if len(numbers) > READ_CHATS:
            return f"NOT RUN: read takes at most {READ_CHATS} chats at once; read the first {READ_CHATS}, then the rest"
        everything, _, _ = await self.conversations()
        chats = []
        for wanted in ([(n, "") for n in numbers] or [(0, text)]):
            chat, why = self.pick(everything, *wanted)
            if chat is None:
                return why
            if chat.get("channel") and not chat.get("followed"):
                return f'NOT RUN: "{chat["title"]}" is not on the chat list (a channel that is not followed): open_chat it'
            if chat["id"] not in [c["id"] for c in chats]:
                chats.append(chat)
        new = [c["id"] for c in chats]
        if front == "list" and all(conv in self.reading for conv in new):
            return ("NOT RUN: App data already shows " + ", ".join(f'"{c["title"]}"' for c in chats)
                    + " above the chat list; open_chat one to go further back in it (older) or to act in it")
        counts = (await self.query(n=("select conversationId, count(*) as stored from Message where conversationId in ("
                                      + ",".join("?" * len(new)) + ") group by conversationId", new)))["n"]
        stored = {row["conversationId"]: row["stored"] for row in counts}
        # Teams loads a conversation only when it is opened: open the ones the phone does not hold whole.
        need = [c for c in chats if not self.whole(c, stored.get(c["id"])) and not self.fresh(c["id"])]
        in_chat, opened, failed = front != "list", [], []
        went = in_chat and not need
        for chat in need:
            if in_chat:
                await self.leave()
            in_chat, state, _ = await self.load(chat)
            (opened if in_chat and state is not None else failed).append(chat)
        if in_chat:
            await self.leave()
        kept = [conv for conv in self.reading if conv not in new]
        gone = kept[:max(0, len(kept) + len(new) - READ_CHATS)]
        self.reading = [*kept[len(gone):], *new]
        titles = {c["id"]: c["title"] for c in everything}

        def names(some):
            return ", ".join(f'"{c["title"]}"' for c in some)

        how = []
        had = [c for c in chats if c not in need and not self.whole(c, stored.get(c["id"]))]
        if opened:
            how.append((f"{names(opened)} " if len(opened) < len(chats) else "")
                       + f"opened so that Teams loaded {'it' if len(opened) == 1 else 'them'} (that marks "
                       f"{'it' if len(opened) == 1 else 'them'} read in Teams)")
        if failed:
            how.append(f"{names(failed)} did not load in {SYNC_WAIT} s: shown as far as the phone has "
                       f"{'it' if len(failed) == 1 else 'them'}")
        if had:
            how.append((f"{names(had)} " if len(had) < len(chats) else "")
                       + "already loaded when it was opened earlier in this session")
        if len(opened) + len(failed) + len(had) < len(chats):
            how.append(("the rest " if how else "") + "read from the phone's data, not opened")
        # Back from a conversation leads where it was opened from: the chat list, or a search's results.
        return (("went back from the conversation; " if went else "") + f"App data now shows {names(chats)}: " + "; ".join(how)
                + (f", beside the {len(kept) - len(gone)} read before" if len(kept) > len(gone) else "")
                + ("; no longer shown (the ones read longest ago): "
                   + ", ".join(f'"{titles.get(conv, "?")}"' for conv in gone) if gone else "")
                + ("; it is shown above Teams' chat list: if Back led elsewhere (a search's results), open the Chat tab"
                   if front != "list" else ""))

    async def leave(self):
        """Back from an open conversation to the chat list (a link sent while one is open does not navigate)."""
        await self.s.driver.device.key("back")
        await asyncio.sleep(0.8)

    async def load(self, chat):
        """Open a conversation by its link and wait until Teams has loaded it: (it opened, its sync state or None
        when Teams did not get there in time, seconds). Loaded: Teams has checked it with its server since, or more
        of it has arrived. One the phone already held whole has nothing to load, and one whose counters meet gets
        SYNC_SETTLE seconds to show otherwise (they also meet for a conversation Teams has never loaded)."""
        was = await self.sync_state(chat["id"]) or {}
        result = await self.s.lane.run({"op": "open", "uri": chat["link"], "package": PACKAGE})
        if not result.get("ok", True):
            return False, result.get("summary") or "no answer", 0
        self.opened, self.view = (chat["id"], time.monotonic()), None
        had_all = number(was.get("seq")) > 0 and number(was.get("stored")) >= number(was.get("seq"))
        started = time.monotonic()
        while (waited := time.monotonic() - started) < SYNC_WAIT:
            state = await self.sync_state(chat["id"]) or {}
            moved = (number(state.get("checked")) > number(was.get("checked"))
                     or number(state.get("stored")) > number(was.get("stored")))
            if moved or (current(state.get("seq"), state.get("synced"), 0, 0) and (had_all or waited >= SYNC_SETTLE)):
                if moved:
                    state = await self.landed(chat["id"], state)
                self.loaded[chat["id"]] = time.monotonic()
                return True, state, time.monotonic() - started
            await asyncio.sleep(0.5 if waited < 3 else 1)
        return True, None, SYNC_WAIT

    async def landed(self, conv, state):
        """The sync state once the page Teams is loading has arrived whole: its count has stopped growing."""
        still = 0
        for _ in range(8):
            await asyncio.sleep(0.6)
            now = await self.sync_state(conv) or state
            still = still + 1 if number(now.get("stored")) <= number(state.get("stored")) else 0
            state = now
            if still >= 2:  # over a second without a new message
                break
        return state

    async def back_to_list(self):
        """The chat list from its start. From an open conversation, Back first (the way open_chat leaves one)."""
        front = self.front(self.s.driver.screen)
        if front == "list":
            self.list_page = 0
            return "App data shows the chat list from its start"
        if front in ("chat", "channel"):
            # The list keeps the page it was on, so what was being worked through is still in front.
            await self.s.driver.device.key("back")
            await asyncio.sleep(0.8)
            return ("went back from the conversation: App data shows the chat list where you left it, once it is in "
                    "front (app_action list again shows it from its start)")
        return "NOT RUN: Teams' chat list is not in front: open Teams' Chat tab first"

    async def open_chat(self, item, text):
        await self.check()
        everything, _, _ = await self.conversations()
        chat, why = self.pick(everything, item, text)
        if chat is None:
            return why
        screen = self.s.driver.screen
        if self.front(screen) in ("chat", "channel"):
            if self.chat == chat["id"]:
                return f'"{chat["title"]}" is already open: App data shows it'
            await self.leave()
        opened, state, seconds = await self.load(chat)
        if not opened:
            return f"FAILED: Teams did not open it: {state}"
        if state is None:
            return (f'opened "{chat["title"]}", but Teams had not synced its newest messages after {SYNC_WAIT} s '
                    "(App data says what it has; Screen shows what Teams draws)")
        return (f'opened "{chat["title"]}" ({chat["kind"]}); Teams synced its newest messages in '
                f"{seconds:.0f} s ({state.get('stored')} on the phone): App data shows them")

    async def older(self):
        conv = self.chat
        screen = self.s.driver.screen
        if self.on_list and not conv:
            if not self.listed or self.list_end >= len(self.listed):
                return ("nothing older: App data's chat list already reached its end (app_action list shows it from "
                        "its start again)")
            self.list_page = self.list_end
            return (f"App data now lists chats {self.list_end + 1}-{min(self.list_end + CHATS_PAGE, len(self.listed))} "
                    f"of {len(self.listed)}")
        if not conv or not screen or not str(screen.header.get("activity") or "").endswith((CHAT_SCREEN, CHANNEL_SCREEN)):
            return "NOT RUN: older works on Teams' chat list and in a chat or channel that App data shows"
        state = await self.sync_state(conv)
        if not state:
            return "FAILED: the conversation is not in Teams' data"
        mark = self.first_shown
        if mark:
            have = (await self.query(n=("select count(*) as n from Message where conversationId=? and arrivalTime < ?",
                                        [conv, mark])))["n"][0]["n"]
            if have:
                self.view = ("before", mark)
                return (f'App data now shows the {min(have, MESSAGES_SHOWN)} messages of "{self.chat_title}" before the '
                        "ones it showed (from the phone)")
        if (state["stored"] or 0) >= (state["seq"] or 0) > 0:
            return "nothing older: App data already shows the start of the conversation"
        if self.topped.get(conv) == (state["stored"] or 0):
            return ("nothing older: older already scrolled this conversation to the top and Teams gave no more; App "
                    "data shows all of it")
        # The phone has nothing older: scroll the conversation up on Teams' interface until it loads the next page.
        stored, flings, started = state["stored"] or 0, 0, time.monotonic()
        x, top, _, bottom = self.s.driver.gesture("down")
        # Each fling passes about ten loaded messages, so the top is this far away; past it, no page means none comes
        # (Teams' counter also counts what it never stores, so "fewer than the counter" is not proof of more).
        budget = max(15, stored // 8 + 10)
        while time.monotonic() - started < OLDER_SECONDS and flings < budget:
            for _ in range(FLINGS_PER_CHECK):
                await self.s.driver.device.swipe(x, top, x, bottom, 60)
            flings += FLINGS_PER_CHECK
            await asyncio.sleep(0.3)
            now = await self.sync_state(conv)
            if now and (now["stored"] or 0) > stored:
                await asyncio.sleep(1)  # the rest of the page
                now = await self.sync_state(conv) or now
                self.view = ("before", mark) if mark else None
                return (f"Teams loaded {now['stored'] - stored} older messages (scrolled up {flings} times, "
                        f"{time.monotonic() - started:.0f} s); now back to {self.when(now['oldest'])}. App data shows them")
        self.topped[conv] = stored
        return (f"no older messages came after scrolling to the top ({flings} flings, {time.monotonic() - started:.0f} s): "
                "this is most likely the start of the conversation (Teams' counter also counts items it does not "
                "keep); App data shows everything the phone has")

    # --- words --------------------------------------------------------------------------------------------

    def when(self, ms):
        if not ms:
            return "?"
        try:
            t = datetime.fromtimestamp(int(ms) / 1000, self.tz)
        except (ValueError, OverflowError, OSError):
            return "?"
        now = datetime.now(self.tz)
        clock = t.strftime("%I:%M %p").lstrip("0")
        days = (now.date() - t.date()).days
        date = f"{t:%a} {t.day} {t:%b}" if t.year == now.year else f"{t:%a} {t.day} {t:%b %Y}"
        # The date always, so a reply never has to work out what "yesterday" was.
        return f"{'today, ' if days == 0 else 'yesterday, ' if days == 1 else ''}{date} {clock}"

    def message_text(self, m, names, files, short=False, posts=None, chars=TEXT_CHARS):
        """One message as "who: text (extras)"; `short` for the chat list's last message. `posts` (a channel's
        messages by id): a post starts with [its subject], a reply says which post it answers."""
        kind_ = m.get("messageType") or ""
        content = m.get("content") or ""
        sender = m.get("sender") or ""
        who = "You" if sender == self.me else (m.get("userDisplayName") or names.get(sender) or "")
        if kind_.startswith("ThreadActivity/"):
            return f"({system(kind_, content, names, self.me)})"
        reply = None
        if m.get("deleteTime") and str(m["deleteTime"]) not in ("0", ""):
            body = "(deleted message)"
        elif kind_ == "Event/Call":
            body = call(content)
        elif kind_ == "RichText/Media_CallRecording":
            found = re.search(r"<Title>(.*?)</Title>", content, re.S)
            body = f"(call recording{': ' + clip(html.unescape(found.group(1)), 80) if found else ''})"
        elif kind_ == "RichText/Media_Card":
            body = card(content)
        else:
            body, reply = plain(content)
            for name in files.get(str(m.get("messageId")), []):
                if name and name not in body:
                    body += f" (file: {name})"
        extras = []
        if posts is not None:
            parent = m.get("parentMessageId")
            if parent and str(parent) != str(m.get("messageId")):
                post = posts.get(parent)
                about = (post.get("subject") or plain(post.get("content"))[0]) if post else ""
                extras.append(f'reply to the post "{clip(about, 60)}"' if about else "reply to an earlier post")
            elif m.get("subject"):
                body = f"[{m['subject']}] {body}"
        if reply and not short:
            extras.append(f'replying to {reply[0] or "?"}' + (f': "{clip(reply[1], 90)}"' if reply[1] else " (a picture or file)"))
        if m.get("editTime") and str(m["editTime"]) not in ("0", "") and not short:
            extras.append("edited")
        if (r := reactions(m.get("emotionCount"))) and not short:
            extras.append(f"reactions: {r}")
        if m.get("mentionsMe") and not short:
            extras.append("mentions you")
        body = clip(body, 160 if short else chars) or f"({kind_ or 'empty message'})"
        return f"{who + ': ' if who else ''}{body}" + (f" [{'; '.join(extras)}]" if extras else "")


# --- helpers ------------------------------------------------------------------------------------------------

def norm(text):
    return " ".join(str(text or "").lower().replace("’", "'").split())


def title(topic, members):
    """A chat's name as Teams' list shows it: its topic, else the other people (1:1: the full name; a group: given
    names, alphabetical, "A, B, and C" or "A, B + 4")."""
    if topic:
        return topic
    if not members:
        return "(unnamed chat)"
    if len(members) == 1 and members[0].get("others", 1) == 1:
        return members[0].get("displayName") or members[0].get("givenName") or "?"
    given = [m.get("givenName") or (m.get("displayName") or "?").split()[0] for m in members]
    others = members[0].get("others") or len(members)
    if others == 2:
        return f"{given[0]} and {given[1]}"
    if others == 3 and len(given) >= 3:
        return f"{given[0]}, {given[1]}, and {given[2]}"
    return f"{given[0]}, {given[1]} + {others - 2}"


def sort_of(c):
    """A conversation's kind in the words the lists use: what it is, whether a group has a name, how many people."""
    return ([c["kind"]] + (["no name set"] if c.get("unnamed") else [])
            + ([f"{c['people']} people"] if c["kind"] != "1:1" and c.get("people") else []))


def kind(conv):
    if conv.startswith("19:meeting_"):
        return "meeting chat"
    if conv.endswith("@unq.gbl.spaces"):
        return "1:1"
    return "group"


TAG = re.compile(r"<[^>]+>")
REPLY = re.compile(r'<blockquote[^>]*itemtype="http://schema\.skype\.com/Reply"[^>]*>(.*?)</blockquote>', re.S | re.I)


def strip(fragment):
    return " ".join(html.unescape(TAG.sub(" ", fragment or "")).replace("\xa0", " ").split())


def plain(content):
    """A message's HTML as one line of text, and (who, what) it replies to, or None."""
    s = content or ""
    reply = None
    if found := REPLY.search(s):
        inner = found.group(1)
        who = re.search(r'<strong[^>]*itemprop="mri"[^>]*>(.*?)</strong>', inner, re.S)
        preview = re.search(r'<p[^>]*itemprop="preview"[^>]*>(.*?)</p>', inner, re.S)
        reply = (strip(who.group(1)) if who else "", strip(preview.group(1)) if preview else "")
        s = s[:found.start()] + s[found.end():]
    s = re.sub(r'<a[^>]*itemtype="http://schema\.skype\.com/HyperLink/Files"[^>]*>(.*?)</a>',
               lambda m: f" (file: {strip(m.group(1))}) ", s, flags=re.S)
    s = re.sub(r'<img[^>]*itemtype="http://schema\.skype\.com/AMSImage"[^>]*>', " (image) ", s)
    s = re.sub(r'<img[^>]*\balt="([^"]*)"[^>]*>', lambda m: m.group(1), s)
    s = re.sub(r'<span[^>]*itemtype="http://schema\.skype\.com/Mention"[^>]*>(.*?)</span>',
               lambda m: "@" + strip(m.group(1)), s, flags=re.S)

    def link(m):
        href, words = html.unescape(m.group(1)), strip(m.group(2))
        return words if not href or words == href or href.rstrip("/").endswith(words.rstrip("/")) else f"{words} ({href})"

    s = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', link, s, flags=re.S)
    s = re.sub(r"<(br|/p|/div|/li|/tr|/h\d|/pre)\b[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<li\b[^>]*>", "• ", s, flags=re.I)
    s = html.unescape(TAG.sub("", s)).replace("\xa0", " ")
    return " / ".join(line for line in (" ".join(part.split()) for part in s.split("\n")) if line), reply


def call(content):
    if "<ended/>" not in content:
        return "(call started)"
    durations = [int(d) for d in re.findall(r"<duration>(\d+)</duration>", content)]
    people = re.search(r'<partlist[^>]*count="(\d+)"', content)
    minutes = round(max(durations) / 60) if durations else 0
    return f"(call ended, {minutes} min, {people.group(1) if people else '?'} people)"


def card(content):
    """An adaptive card's words."""
    found = []

    def walk(value):
        if isinstance(value, dict):
            for k, v in value.items():
                if k in ("text", "altText", "title") and isinstance(v, str) and v.strip() and v not in found:
                    found.append(v.strip())
                else:
                    walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    try:
        walk(json.loads(content))
    except ValueError:
        return "(card)"
    return f"(card: {clip(' · '.join(found), 300)})" if found else "(card)"


def reactions(raw):
    try:
        counts = json.loads(raw) if raw else {}
    except ValueError:
        return ""
    if not isinstance(counts, dict):
        return ""
    return ", ".join(f"{k.split(';')[0]} {v}" for k, v in counts.items()
                     if k not in ("messageId", "myEmotions") and isinstance(v, int) and v)


def system(kind_, content, names, me):
    """A membership or topic event in a few words."""
    def name(mri):
        return "you" if mri == me else names.get(mri) or "someone"

    who = name(m.group(1)) if (m := re.search(r"<initiator>(.*?)</initiator>", content)) else "someone"
    targets = ", ".join(name(t) for t in re.findall(r"<target>(.*?)</target>", content)) or "someone"
    sub = kind_.split("/", 1)[1]
    if sub == "AddMember":
        return f"{who} added {targets}"
    if sub == "DeleteMember":
        return f"{who} removed {targets}"
    if sub == "MemberJoined":
        return f"{targets} joined"
    if sub == "MemberLeft":
        return f"{targets} left"
    if sub == "TopicUpdate":
        value = re.search(r"<value>(.*?)</value>", content, re.S)
        return f'{who} renamed the chat to "{html.unescape(value.group(1)) if value else "?"}"'
    return re.sub(r"(?<!^)(?=[A-Z])", " ", sub).lower()


def unread_differs(screen, conversations):
    """The chats whose row on screen says otherwise than App data about unread messages ("Billing v3. Group chat. New
    messages. Last message at ..."): a check of the read mark against what Teams draws."""
    by_name = {}
    for c in conversations:
        for name in c["names"]:
            by_name.setdefault(name, c)
    differ = []
    for el in screen.elements.values():
        if el.node.get("id") != "chat_item_container" or not el.label:
            continue
        c = by_name.get(norm(el.label.split(". ", 1)[0]))
        if c is not None and ("New messages" in el.label) != bool(c["unread_n"]):
            differ.append(f'"{c["title"]}" (Teams: {"unread" if "New messages" in el.label else "read"})')
    return differ


def texts_by_id(screen, resource_id):
    """The words of every node on screen with this resource id."""
    found = []

    def walk(node):
        if node.get("id") == resource_id and (node.get("text") or node.get("desc")):
            found.append(node.get("text") or node.get("desc"))
        for child in node.get("c", []):
            walk(child)

    for window in screen.snapshot.get("windows", []):
        for node in window.get("nodes", []):
            walk(node)
    return found


SAFE_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ,.?!:()\-]{15,}")


def anchors(screen, most=6):
    """Pieces of the messages on screen that the database has word for word (no mention, entity or tag inside):
    a stretch of plain words from each message bubble, at most `most`. Teams' own lines for membership events
    ("X added Y to the conversation") are drawn outside bubbles (a chat's rich_text_layout, a channel's
    message_content) and stored differently, so they are not used."""
    found = []
    for words in texts_inside(screen, "rich_text_layout") + texts_inside(screen, "message_content"):
        runs = sorted(SAFE_RUN.findall(words), key=len, reverse=True)
        if runs:
            piece = " ".join(runs[0].split())[-40:].strip()
            if len(piece) >= 16 and piece not in found:
                found.append(piece)
        if len(found) >= most:
            break
    return found


def texts_inside(screen, holder_id):
    """The words of text nodes inside every node with this resource id (a chat's message bubbles)."""
    found = []

    def walk(node, inside):
        inside = inside or node.get("id") == holder_id
        text = node.get("text")
        if inside and text and len(text) >= 16:
            found.append(text)
        for child in node.get("c", []):
            walk(child, inside)

    for window in screen.snapshot.get("windows", []):
        for node in window.get("nodes", []):
            walk(node, False)
    return found


def fit(text, keep_end=False, limit=BLOCK_CHARS):
    """The block within `limit` characters: a chat keeps its newest lines, a list its first."""
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    head = lines[:3] if keep_end else []
    body = lines[3:] if keep_end else lines
    out, size = [], sum(len(line) + 1 for line in head)
    for line in (reversed(body) if keep_end else body):
        if size + len(line) + 1 > limit:
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(head + (list(reversed(out)) if keep_end else out) + ["(cut here: more than fits)"])


__all__ = ["Teams", "HighwayError"]
