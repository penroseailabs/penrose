package app.relay.home;

import android.content.AttributionSource;
import android.database.ContentObserver;
import android.database.Cursor;
import android.net.Uri;
import android.os.Binder;
import android.os.Bundle;
import android.os.IBinder;
import android.os.Parcel;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;

/**
 * Gmail's server search, as root, in a small process of its own that stays up between searches: Gmail.java
 * starts it through su (`app_process` with this APK as the class path) and talks to it over stdin/stdout, one
 * JSON line each way:
 *
 *   {"id":1,"q":"doordash receipt","limit":25,"wait_ms":6000}  ->  {"id":1,"emails":[...],"server":true,"error":null,"ms":1210}
 *
 * `wait_ms` is how long to wait for Gmail's servers (WAIT_MS at most) before the phone's own matches go back alone.
 *
 * It asks Gmail's content providers directly, the way Android's `content` tool does, but without starting a
 * new process for every query (each costs about a second). The search, by column name in the mail contract
 * Gmail serves (UIProvider): each account's searchUri (gm2.accountcache lists them), the search's
 * conversationListUri, and the list's conversation columns. The list first holds the matches already on the
 * phone; Gmail signals the list when its servers answer, and the extras flag cursor_received_server_results.
 * The sender is in each row's conversationInfo; if a Gmail update changes that record, it comes from the
 * conversation's message list instead (slower: Gmail fetches the message).
 *
 * It exits after IDLE_MS without a request, and when stdin closes (the home app went away).
 */
public final class GmailHelper {
    static final long IDLE_MS = 300_000;  // a task's searches come minutes apart: each would start it again
    private static final long WAIT_MS = 6_000;       // for Gmail's servers, then the phone's matches alone
    private static final long WAIT_UNKNOWN_MS = 2_500; // the same when the extras do not say (a changed Gmail)
    private static final long POLL_MS = 300;
    private static final long SENDER_BUDGET_MS = 3_000;
    private static final int MAX_QUERY = 500;
    private static final String ACCOUNTS = "content://com.google.android.gm2.accountcache/accounts";
    private static final String SERVER_FLAG = "cursor_received_server_results";
    private static final String[] LIST_COLUMNS = {"conversationUri", "dateReceivedMs", "read", "starred",
        "hasAttachments", "numMessages", "subject", "snippet", "conversationInfo"};

    private final Object activityManager;
    private final Method getProvider, releaseProvider, query;
    private final boolean attributionQuery;
    private final IBinder token = new Binder();
    private final Map<String, Object> providers = new HashMap<>();
    private final PrintStream out;
    private volatile long lastActive = System.currentTimeMillis();
    private volatile boolean busy;

    public static void main(String[] args) {
        PrintStream out = new PrintStream(new java.io.FileOutputStream(java.io.FileDescriptor.out), true);
        GmailHelper helper;
        try {
            helper = new GmailHelper(out);
        } catch (Throwable t) {
            out.println(fail(0, "this phone's Android has no provider access the helper knows (" + t.getClass().getSimpleName() + ")"));
            System.exit(1);
            return;
        }
        helper.serve();
        helper.releaseAll();
        System.exit(0);
    }

    private GmailHelper(PrintStream out) throws Exception {
        this.out = out;
        activityManager = Class.forName("android.app.ActivityManager").getMethod("getService").invoke(null);
        Class<?> am = activityManager.getClass();
        getProvider = am.getMethod("getContentProviderExternal", String.class, int.class, IBinder.class, String.class);
        releaseProvider = am.getMethod("removeContentProviderExternalAsUser", String.class, IBinder.class, int.class);
        Class<?> provider = Class.forName("android.content.IContentProvider");
        Class<?> cancel = Class.forName("android.os.ICancellationSignal");
        Method q;
        boolean attribution = true;
        try {  // Android 12+
            q = provider.getMethod("query", AttributionSource.class, Uri.class, String[].class, Bundle.class, cancel);
        } catch (NoSuchMethodException e) {  // Android 11
            q = provider.getMethod("query", String.class, String.class, Uri.class, String[].class, Bundle.class, cancel);
            attribution = false;
        }
        query = q;
        attributionQuery = attribution;
    }

    private void serve() {
        Thread idle = new Thread(() -> {
            while (true) {
                try { Thread.sleep(5_000); } catch (InterruptedException e) { return; }
                if (!busy && System.currentTimeMillis() - lastActive > IDLE_MS) {
                    releaseAll();
                    System.exit(0);
                }
            }
        }, "relay-gmail-idle");
        idle.setDaemon(true);
        idle.start();
        // Connect to Gmail's providers now, while the query is still being typed: the first connection is the slow part.
        Thread connect = new Thread(() -> {
            try (Cursor c = query(Uri.parse(ACCOUNTS), new String[]{"searchUri"})) {
                while (c != null && c.moveToNext()) {
                    String s = string(c, "searchUri");
                    if (s != null) provider(Uri.parse(s).getAuthority());
                }
            } catch (Exception ignored) {
                // the search reports it
            }
        }, "relay-gmail-connect");
        connect.setDaemon(true);
        connect.start();
        try (BufferedReader in = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8))) {
            String line;
            while ((line = in.readLine()) != null) {
                if (line.trim().isEmpty()) continue;
                busy = true;
                try {
                    out.println(handle(line));
                } finally {
                    busy = false;
                    lastActive = System.currentTimeMillis();
                }
            }
        } catch (Exception ignored) {
            // stdin closed or broken: the home app is gone
        }
    }

    private String handle(String line) {
        long start = System.currentTimeMillis();
        int id = 0;
        try {
            JSONObject r = new JSONObject(line);
            id = r.optInt("id");
            if (!"search".equals(r.optString("cmd", "search"))) return fail(id, "unknown cmd");
            String q = r.optString("q").trim();
            if (q.isEmpty() || q.length() > MAX_QUERY) return fail(id, "no query");
            int limit = Math.max(1, Math.min(r.optInt("limit", 25), 50));
            long wait = Math.max(500, Math.min(r.optLong("wait_ms", WAIT_MS), WAIT_MS));
            JSONObject answer = search(q, limit, wait);
            answer.put("id", id);
            answer.put("ms", System.currentTimeMillis() - start);
            return answer.toString();
        } catch (Throwable t) {
            return fail(id, "the search failed (" + t.getClass().getSimpleName() + ")");
        }
    }

    // ---- the search ----

    private static final class Account {
        String searchUri, mimeType, name;
    }

    private static final class Found {
        final List<JSONObject> emails = new ArrayList<>();
        boolean server;
        String error;
        String steps = "";  // timings, for diagnosis: "list 40 · server 1150 (4 reads) · rows 30"
    }

    private JSONObject search(String q, int limit, long wait) throws Exception {
        List<Account> accounts = new ArrayList<>();
        try (Cursor c = query(Uri.parse(ACCOUNTS), new String[]{"searchUri", "mimeType", "name"})) {
            if (c == null) return failObject("Gmail is not installed or not set up");
            while (c.moveToNext()) {
                Account a = new Account();
                a.searchUri = string(c, "searchUri");
                a.mimeType = string(c, "mimeType");
                a.name = string(c, "name");
                if (a.searchUri != null) accounts.add(a);
            }
        }
        if (accounts.isEmpty()) return failObject("no account in Gmail");

        // Accounts side by side: each waits on its own server answer.
        List<Found> found = new ArrayList<>();
        List<Thread> threads = new ArrayList<>();
        for (Account a : accounts) {
            Found f = new Found();
            found.add(f);
            Thread t = new Thread(() -> {
                try {
                    searchAccount(a, q, limit, wait, f);
                } catch (Throwable e) {
                    f.error = "the search failed (" + e.getClass().getSimpleName() + ")";
                }
            }, "relay-gmail-account");
            threads.add(t);
            t.start();
        }
        for (Thread t : threads) t.join(wait + SENDER_BUDGET_MS + 3_000);

        List<JSONObject> emails = new ArrayList<>();
        boolean server = true;
        String error = null;
        List<String> steps = new ArrayList<>();
        for (Found f : found) {
            synchronized (f) {
                steps.add(f.steps);
                emails.addAll(f.emails);
                server &= f.server;
                if (error == null) error = f.error;
            }
        }
        emails.sort((x, y) -> Long.compare(y.optLong("date"), x.optLong("date")));
        JSONArray list = new JSONArray();
        for (int i = 0; i < Math.min(limit, emails.size()); i++) list.put(emails.get(i));
        JSONObject answer = new JSONObject();
        answer.put("emails", list);
        answer.put("server", server);
        answer.put("steps", String.join(" | ", steps));
        answer.put("error", emails.isEmpty() && error != null ? error : JSONObject.NULL);
        return answer;
    }

    private void searchAccount(Account a, String q, int limit, long wait, Found f) throws Exception {
        long t0 = System.currentTimeMillis();
        Uri search = Uri.parse(a.searchUri).buildUpon().appendQueryParameter("query", q)
            .appendQueryParameter("query_identifier", String.valueOf(System.nanoTime())).build();
        String listUri;
        try (Cursor c = query(search, new String[]{"conversationListUri"})) {
            listUri = c != null && c.moveToFirst() ? string(c, "conversationListUri") : null;
        }
        if (listUri == null) {
            synchronized (f) { f.error = "the search gave no result list"; }
            return;
        }
        Uri list = Uri.parse(listUri);

        // The first read is what the phone already has; wait for Gmail's servers, re-reading on its signal
        // (or every POLL_MS: the signal comes about once a second).
        final Semaphore changed = new Semaphore(0);
        Cursor watched = query(list, LIST_COLUMNS);
        if (watched == null) {
            synchronized (f) { f.error = "the result list did not open"; }
            return;
        }
        Cursor latest = watched;
        try {
            watched.registerContentObserver(new ContentObserver(null) {
                @Override public void onChange(boolean selfChange) { changed.release(); }
            });
            long started = System.currentTimeMillis();
            long deadline = started + (serverFlag(watched) == null ? Math.min(WAIT_UNKNOWN_MS, wait) : wait);
            int reads = 1;
            while (!Boolean.TRUE.equals(serverFlag(latest))) {
                long left = deadline - System.currentTimeMillis();
                if (left <= 0) break;
                changed.tryAcquire(Math.min(POLL_MS, left), TimeUnit.MILLISECONDS);
                Cursor next = query(list, LIST_COLUMNS);
                if (next == null) break;
                if (latest != watched) latest.close();
                latest = next;
                reads++;
            }
            boolean server = Boolean.TRUE.equals(serverFlag(latest));
            long answered = System.currentTimeMillis();
            List<JSONObject> rows = rows(latest, a, listUri, limit);
            synchronized (f) {
                f.emails.addAll(rows);
                f.server = server;
                f.steps = "list " + (started - t0) + " · " + (server ? "server " : "no server ") + (answered - started)
                    + " (" + reads + " reads) · rows " + (System.currentTimeMillis() - answered);
            }
        } finally {
            if (latest != watched) latest.close();
            watched.close();
        }
    }

    /** The newest `limit` conversations of a result list, with their senders. */
    private List<JSONObject> rows(Cursor c, Account a, String listUri, int limit) throws Exception {
        List<JSONObject> rows = new ArrayList<>();
        Map<JSONObject, byte[]> info = new HashMap<>();
        c.moveToPosition(-1);
        while (c.moveToNext()) {
            String uri = string(c, "conversationUri");
            if (uri == null) continue;
            JSONObject e = new JSONObject();
            e.put("uri", uri);
            e.put("date", number(c, "dateReceivedMs"));
            e.put("read", number(c, "read") != 0);
            e.put("starred", number(c, "starred") != 0);
            e.put("attachments", number(c, "hasAttachments") != 0);
            e.put("messages", number(c, "numMessages"));
            e.put("subject", orNull(string(c, "subject")));
            e.put("snippet", orNull(string(c, "snippet")));
            e.put("account", orNull(a.name));
            e.put("type", orNull(a.mimeType));
            e.put("folder", listUri);
            rows.add(e);
            int i = c.getColumnIndex("conversationInfo");
            if (i >= 0 && c.getType(i) == Cursor.FIELD_TYPE_BLOB) info.put(e, c.getBlob(i));
        }
        rows.sort((x, y) -> Long.compare(y.optLong("date"), x.optLong("date")));
        if (rows.size() > limit) rows = new ArrayList<>(rows.subList(0, limit));

        List<JSONObject> missing = new ArrayList<>();
        for (JSONObject e : rows) {
            String from = sender(info.get(e), e.optInt("messages"));
            if (from != null) e.put("from", from);
            else missing.add(e);
        }
        if (!missing.isEmpty()) sendersFromMessages(missing);
        return rows;
    }

    /**
     * The sender in a conversationInfo record: message count, draft count, three snippets, then the sender's
     * name and address (Parcel layout, as in AOSP's mail ConversationInfo). Null when the record does not
     * check out (its count must be the row's numMessages, the address must hold an @): a layout this code
     * does not know, and the caller asks the message list instead.
     */
    static String sender(byte[] blob, int messages) {
        if (blob == null || blob.length < 8) return null;
        Parcel p = Parcel.obtain();
        try {
            p.unmarshall(blob, 0, blob.length);
            p.setDataPosition(0);
            int count = p.readInt();
            p.readInt();
            p.readString();
            p.readString();
            p.readString();
            String name = p.readString(), email = p.readString();
            if (count != messages || email == null || !email.contains("@") || email.length() > 320) return null;
            name = name == null ? "" : name.trim();
            return name.isEmpty() ? email.trim() : "\"" + name + "\" <" + email.trim() + ">";
        } catch (RuntimeException e) {
            return null;
        } finally {
            p.recycle();
        }
    }

    /** The fallback: each conversation's last message's fromAddress, four at a time, within SENDER_BUDGET_MS. */
    private void sendersFromMessages(List<JSONObject> rows) {
        ExecutorService pool = Executors.newFixedThreadPool(Math.min(4, rows.size()));
        try {
            List<Future<String>> asks = new ArrayList<>();
            for (JSONObject e : rows) {
                String uri = e.optString("uri");
                asks.add(pool.submit(() -> {
                    try (Cursor m = query(Uri.parse(uri), new String[]{"fromAddress"})) {
                        return m != null && m.moveToLast() ? string(m, "fromAddress") : null;
                    }
                }));
            }
            long deadline = System.currentTimeMillis() + SENDER_BUDGET_MS;
            for (int i = 0; i < rows.size(); i++) {
                try {
                    String from = asks.get(i).get(Math.max(1, deadline - System.currentTimeMillis()), TimeUnit.MILLISECONDS);
                    if (from != null && !from.isEmpty()) rows.get(i).put("from", from);
                } catch (Exception ignored) {
                    // too slow or failed: no sender for this one
                }
            }
        } finally {
            pool.shutdownNow();
        }
    }

    // ---- provider access ----

    /** A query on the provider for uri's authority; a provider that died (Gmail restarted) is fetched again once. */
    private Cursor query(Uri uri, String[] projection) throws Exception {
        for (int attempt = 0; ; attempt++) {
            Object provider = provider(uri.getAuthority());
            if (provider == null) return null;
            try {
                Object c = attributionQuery
                    ? query.invoke(provider, new AttributionSource.Builder(android.os.Process.myUid()).setPackageName("root").build(),
                        uri, projection, null, null)
                    : query.invoke(provider, "root", null, uri, projection, null, null);
                return (Cursor) c;
            } catch (InvocationTargetException e) {
                if (attempt == 0 && e.getCause() instanceof android.os.DeadObjectException) {
                    synchronized (providers) { providers.remove(uri.getAuthority()); }
                    continue;
                }
                Throwable cause = e.getCause();
                if (cause instanceof Exception) throw (Exception) cause;
                throw e;
            }
        }
    }

    private Object provider(String authority) throws Exception {
        synchronized (providers) {
            Object p = providers.get(authority);
            if (p != null) return p;
            Object holder = getProvider.invoke(activityManager, authority, 0, token, "relay-home");
            if (holder == null) return null;
            p = holder.getClass().getField("provider").get(holder);
            if (p != null) providers.put(authority, p);
            return p;
        }
    }

    private void releaseAll() {
        synchronized (providers) {
            for (String authority : providers.keySet()) {
                try {
                    releaseProvider.invoke(activityManager, authority, token, 0);
                } catch (Exception ignored) {
                }
            }
            providers.clear();
        }
    }

    // ---- helpers ----

    /** The extras' word on whether Gmail's servers have answered; null when the extras do not say. */
    private static Boolean serverFlag(Cursor c) {
        try {
            Bundle b = c.getExtras();
            if (b == null || !b.containsKey(SERVER_FLAG)) return null;
            return b.getBoolean(SERVER_FLAG);  // only this key: others hold Gmail's own classes
        } catch (RuntimeException e) {
            return null;
        }
    }

    private static String string(Cursor c, String column) {
        int i = c.getColumnIndex(column);
        return i < 0 || c.isNull(i) ? null : c.getString(i);
    }

    private static long number(Cursor c, String column) {
        int i = c.getColumnIndex(column);
        try {
            return i < 0 || c.isNull(i) ? 0 : c.getLong(i);
        } catch (RuntimeException e) {
            return 0;
        }
    }

    private static Object orNull(String s) { return s == null ? JSONObject.NULL : s; }

    private static JSONObject failObject(String why) throws Exception {
        JSONObject o = new JSONObject();
        o.put("emails", new JSONArray());
        o.put("server", false);
        o.put("error", why);
        return o;
    }

    private static String fail(int id, String why) {
        try {
            JSONObject o = failObject(why);
            o.put("id", id);
            return o.toString();
        } catch (Exception e) {
            return "{\"id\":" + id + ",\"emails\":[],\"error\":\"failed\"}";
        }
    }
}
