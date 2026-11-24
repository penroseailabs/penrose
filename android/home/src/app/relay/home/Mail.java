package app.relay.home;

import android.content.Context;
import android.content.pm.ApplicationInfo;
import android.text.format.DateFormat;
import android.text.format.DateUtils;

import java.util.ArrayList;
import java.util.Date;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

/**
 * Gmail for the query: the emails Gmail's own server search finds (Gmail.search, in the background), and a
 * row that opens Gmail with the search already run. Searching takes Gmail one to two seconds, and several
 * right after the phone has been asleep, so a query's answer is fetched once (`fetch`) and kept for two
 * minutes; until it is in, the open row says so.
 *
 * Gmail runs one search at a time, so the queries wait their turn in one queue, in the order they were asked.
 * Nobody waits on that queue longer than they said they would: `fetch` returns at its caller's deadline, the
 * search goes on, and its answer is there for whoever asks next. An answer without Gmail's servers (the
 * phone's own matches, or a failure) is kept for a few seconds only, so the next search asks again.
 */
final class Mail implements Source {
    static final String GMAIL = Gmail.PACKAGE;
    static final int LIMIT = 25;
    private static final long KEEP_MS = 120_000;
    private static final long KEEP_PARTIAL_MS = 10_000;
    /** How it stands with Gmail for a query, as the agent's search says it (`mail`). */
    static final String OFF = "off", ANSWERED = "answered", PHONE_ONLY = "phone_only", FAILED = "failed",
        PENDING = "pending";

    private final Context context;
    private final Map<String, Gmail.Answer> answers = new LinkedHashMap<String, Gmail.Answer>(16, 0.75f, true) {
        @Override protected boolean removeEldestEntry(Map.Entry<String, Gmail.Answer> e) { return size() > 20; }
    };
    private final Map<String, Future<?>> asking = new HashMap<>();
    private final ExecutorService queue = Executors.newSingleThreadExecutor(r -> {
        Thread t = new Thread(r, "relay-home-mail");
        t.setDaemon(true);
        return t;
    });

    Mail(Context context) { this.context = context; }

    @Override
    public void search(Query q, List<Result> out) {
        if (q.isEmpty() || !enabled()) return;
        Gmail.Answer a = answer(q);
        // "no emails found" only when Gmail's servers said so: without their answer, nobody knows.
        String state = a == null ? (asking(q) ? "NOT SEARCHED: Gmail has not answered yet" : "looking for emails…")
            : a.server ? (a.emails.isEmpty() ? "no emails found" : a.emails.size() + " emails")
            : !a.emails.isEmpty() ? "NOT SEARCHED: Gmail's servers did not answer; only the " + a.emails.size() + " emails stored on the phone are listed"
            : a.error != null ? "NOT SEARCHED: " + a.error : "NOT SEARCHED: Gmail's servers did not answer";
        // Above the web searches, below anything on the phone that matched by name.
        out.add(new Result(Result.MAIL, q.raw, "search Gmail · " + state, 3, q.raw, "open"));
        if (a == null) return;
        for (int i = 0; i < a.emails.size(); i++) {
            Gmail.Email e = a.emails.get(i);
            Result r = new Result(Result.MAIL, e.subject == null || e.subject.isEmpty() ? "(no subject)" : e.subject,
                describe(e), 4, e, "open");
            r.order = i;  // newest first, as Gmail gave them
            out.add(r);
        }
    }

    /** Whether `q` still needs Gmail's answer. */
    boolean pending(Query q) {
        return !q.isEmpty() && enabled() && answer(q) == null;
    }

    /** Get Gmail's helper up ahead of `fetch` while the query is typed (it exits on its own when idle). */
    void warm(Query q) {
        if (pending(q)) Gmail.warm(context);
    }

    /**
     * Ask Gmail and keep the answer: back when it is in, or after `waitMs` at the latest. A search that is
     * still going then goes on, in its turn, and its answer is kept for the next search of the same words.
     */
    void fetch(Query q, long waitMs) {
        Future<?> search;
        synchronized (asking) {
            if (!pending(q)) return;
            String key = key(q);
            search = asking.get(key);
            if (search == null) {
                search = queue.submit(() -> {
                    try {
                        Gmail.Answer a = Gmail.search(context, q.raw, LIMIT);
                        synchronized (answers) { answers.put(key, a); }
                    } finally {
                        synchronized (asking) { asking.remove(key); }
                    }
                });
                asking.put(key, search);
            }
        }
        try {
            search.get(Math.max(1, waitMs), TimeUnit.MILLISECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        } catch (Exception ignored) {
            // not in by the deadline (the row says so), or the search itself failed (its answer says why)
        }
    }

    /** Forget an answer Gmail's servers did not give, so the next fetch asks them again. */
    void retry(Query q) {
        synchronized (answers) {
            Gmail.Answer a = answers.get(key(q));
            if (a != null && !a.server) answers.remove(key(q));
        }
    }

    /** OFF (no Gmail here), ANSWERED by Gmail's servers, PHONE_ONLY, FAILED, or PENDING: not in yet. */
    String state(Query q) {
        if (q.isEmpty() || !enabled()) return OFF;
        Gmail.Answer a = answer(q);
        return a == null ? PENDING : a.server ? ANSWERED : a.emails.isEmpty() ? FAILED : PHONE_ONLY;
    }

    private boolean asking(Query q) {
        synchronized (asking) { return asking.containsKey(key(q)); }
    }

    private Gmail.Answer answer(Query q) {
        synchronized (answers) {
            Gmail.Answer a = answers.get(key(q));
            if (a != null && System.currentTimeMillis() - a.at > (a.server ? KEEP_MS : KEEP_PARTIAL_MS)) {
                answers.remove(key(q));
                return null;
            }
            return a;
        }
    }

    private static String key(Query q) { return q.raw.trim().toLowerCase(Locale.ROOT); }

    /** `from DoorDash · Oct 22 · unread · attachment · Your membership is active…` */
    private String describe(Gmail.Email e) {
        List<String> parts = new ArrayList<>();
        String from = name(e.from);
        if (from != null) parts.add("from " + from);  // reads "Mail from Amazon"
        if (e.date > 0) parts.add(when(e.date));
        if (!e.read) parts.add("unread");
        if (e.starred) parts.add("starred");
        if (e.attachments) parts.add("attachment");
        if (e.messages > 1) parts.add(e.messages + " messages");
        if (e.snippet != null && !e.snippet.trim().isEmpty()) parts.add(clip(e.snippet.trim(), 90));
        return String.join(" · ", parts);
    }

    /** `"DoorDash" <no-reply@doordash.com>` reads DoorDash; a bare address stays an address. */
    static String name(String from) {
        if (from == null || from.trim().isEmpty()) return null;
        String f = from.trim();
        int lt = f.indexOf('<');
        if (lt > 0) {
            String n = f.substring(0, lt).trim().replaceAll("^\"|\"$", "").trim();
            if (!n.isEmpty()) return n;
        }
        return f.replaceAll("[<>]", "");
    }

    private String when(long ms) {
        Locale l = Locale.getDefault();
        if (DateUtils.isToday(ms)) return android.text.format.DateFormat.getTimeFormat(context).format(new Date(ms));
        java.util.Calendar now = java.util.Calendar.getInstance(), then = java.util.Calendar.getInstance();
        then.setTimeInMillis(ms);
        String skeleton = now.get(java.util.Calendar.YEAR) == then.get(java.util.Calendar.YEAR) ? "MMMd" : "MMMdyyyy";
        return new java.text.SimpleDateFormat(DateFormat.getBestDateTimePattern(l, skeleton), l).format(new Date(ms));
    }

    private static String clip(String s, int n) {
        // Marketing mail pads its preview with invisible characters (zero-width, combining, soft hyphen).
        s = s.replaceAll("[\\u034F\\u00AD\\u180E\\u200B-\\u200F\\u2060-\\u2064\\uFEFF]", "").replaceAll("\\s+", " ").trim();
        return s.length() <= n ? s : s.substring(0, n).trim() + "…";
    }

    private boolean enabled() {
        try {
            ApplicationInfo ai = context.getPackageManager().getApplicationInfo(GMAIL, 0);
            return ai.enabled;
        } catch (Exception e) {
            return false;
        }
    }
}
