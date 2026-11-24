package app.relay.home;

import android.content.Context;
import android.provider.Settings;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.ReentrantLock;

/**
 * Gmail's own server search, in the background: nothing comes on screen. Gmail runs a live search (its
 * servers, not just the mail synced to the phone) only for callers outside its process, so it runs as root, in
 * GmailHelper: a small process started through su from this APK, kept up between searches and gone after a
 * minute without one (GmailHelper.IDLE_MS), so it costs no memory while the phone is not searching mail.
 * `warm` starts it while the query is still being typed, so the search itself pays no start-up.
 *
 * Root comes from Relay's phone setup (a Magisk policy for this app, no toast), which also sets the global
 * setting `relay_home_root` = 1. Without that setting no su is run: an ungranted su would put Magisk's prompt on
 * screen. The query reaches the helper as a JSON string on its stdin, never through a shell.
 */
final class Gmail {
    static final String PACKAGE = "com.google.android.gm";
    static final String ROOT_SETTING = "relay_home_root";
    private static final long TIMEOUT_MS = 15_000;
    private static final String EOF = "\u0000eof";
    private static final ReentrantLock LOCK = new ReentrantLock();

    /** Gmail's servers are waited for this long, and this long while they have just failed to answer. */
    private static final long SERVER_WAIT_MS = 6_000, SERVER_WAIT_AGAIN_MS = 2_500, UNANSWERED_FOR_MS = 30_000;

    private static long unanswered;  // when Gmail's servers last did not answer a search (LOCK held)
    private static Process helper;
    private static OutputStream toHelper;
    private static LinkedBlockingQueue<String> fromHelper;
    private static int nextId;

    static final class Email {
        String subject, snippet, from, account, uri, type, folder;
        long date;
        boolean read, starred, attachments;
        int messages;
    }

    /** What one search gave: the emails, or why there are none. `server`: Gmail's servers answered (not just the phone). */
    static final class Answer {
        final List<Email> emails;
        final String error;
        final boolean server;
        final long at = System.currentTimeMillis();

        Answer(List<Email> emails, String error, boolean server) {
            this.emails = emails;
            this.error = error;
            this.server = server;
        }

        static Answer failed(String why) { return new Answer(new ArrayList<>(), why, false); }
    }

    static boolean rootGranted(Context ctx) {
        return Settings.Global.getInt(ctx.getContentResolver(), ROOT_SETTING, 0) == 1;
    }

    /** Start the helper ahead of a search, if it is not up. Returns at once (a search under way has it up). */
    static void warm(Context ctx) {
        if (!rootGranted(ctx) || !LOCK.tryLock()) return;
        try {
            ensure(ctx);
        } catch (Exception ignored) {
            // the search reports it
        } finally {
            LOCK.unlock();
        }
    }

    static Answer search(Context ctx, String query, int limit) {
        if (!rootGranted(ctx)) return Answer.failed("needs root for the penrose home app (penrose's phone setup)");
        String q = query.trim();
        if (q.isEmpty()) return Answer.failed("no query");
        LOCK.lock();
        try {
            // Twice at most: a helper that was up may have just left (idle) as the request went in.
            for (int attempt = 0; attempt < 2; attempt++) {
                boolean fresh;
                try {
                    fresh = ensure(ctx);
                } catch (Exception e) {
                    stop();
                    return Answer.failed("root was not available (" + e.getClass().getSimpleName() + ")");
                }
                int id = ++nextId;
                try {
                    JSONObject r = new JSONObject();
                    r.put("id", id);
                    r.put("cmd", "search");
                    r.put("q", q);
                    r.put("limit", limit);
                    // Servers that did not answer the last search rarely answer the next one a moment later: the
                    // queries behind it get the phone's matches sooner, and the servers' as soon as they are back.
                    r.put("wait_ms", System.currentTimeMillis() - unanswered < UNANSWERED_FOR_MS ? SERVER_WAIT_AGAIN_MS : SERVER_WAIT_MS);
                    toHelper.write((r.toString() + "\n").getBytes(StandardCharsets.UTF_8));
                    toHelper.flush();
                } catch (Exception e) {
                    stop();
                    if (fresh) return Answer.failed("the Gmail helper did not start");
                    continue;
                }
                List<String> other = new ArrayList<>();
                long deadline = System.currentTimeMillis() + TIMEOUT_MS;
                while (true) {
                    String line;
                    try {
                        line = fromHelper.poll(Math.max(1, deadline - System.currentTimeMillis()), TimeUnit.MILLISECONDS);
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                        return Answer.failed("interrupted");
                    }
                    if (line == null) {
                        stop();  // stuck: the next search starts a new one
                        return Answer.failed("Gmail took too long");
                    }
                    if (line.equals(EOF)) {
                        stop();
                        if (!fresh) break;  // try once more with a new helper
                        return Answer.failed(other.isEmpty() ? "root was refused" : "root was refused: " + other.get(0));
                    }
                    if (!line.startsWith("{")) {
                        if (other.size() < 5) other.add(line.trim());
                        continue;
                    }
                    try {
                        JSONObject a = new JSONObject(line);
                        if (a.optInt("id") != id) continue;  // an answer to an earlier request that timed out
                        Answer answer = parse(a, limit);
                        unanswered = answer.server ? 0 : System.currentTimeMillis();
                        return answer;
                    } catch (Exception e) {
                        return Answer.failed("the Gmail helper's answer did not read");
                    }
                }
            }
            return Answer.failed("the Gmail helper stopped");
        } finally {
            LOCK.unlock();
        }
    }

    /** Whether a new helper was started (false: the one that was up is used). Call with LOCK held. */
    private static boolean ensure(Context ctx) throws Exception {
        if (helper != null && helper.isAlive()) return false;
        stop();
        String apk = ctx.getApplicationInfo().sourceDir;
        if (apk == null || !apk.matches("[A-Za-z0-9/._=~+-]+")) throw new IllegalStateException("apk path");
        String cmd = "export CLASSPATH='" + apk + "'; exec app_process /system/bin --nice-name=relay-gmail " + GmailHelper.class.getName();
        Process p = new ProcessBuilder("su", "-c", cmd).redirectErrorStream(true).start();
        LinkedBlockingQueue<String> lines = new LinkedBlockingQueue<>();
        Thread reader = new Thread(() -> {
            try (BufferedReader out = new BufferedReader(new InputStreamReader(p.getInputStream(), StandardCharsets.UTF_8))) {
                String line;
                while ((line = out.readLine()) != null) lines.add(line);
            } catch (Exception ignored) {
            }
            lines.add(EOF);
        }, "relay-gmail-out");
        reader.setDaemon(true);
        reader.start();
        helper = p;
        toHelper = p.getOutputStream();
        fromHelper = lines;
        return true;
    }

    /** Call with LOCK held. */
    private static void stop() {
        if (helper != null) helper.destroy();
        helper = null;
        toHelper = null;
        fromHelper = null;
    }

    private static Answer parse(JSONObject a, int limit) {
        List<Email> emails = new ArrayList<>();
        JSONArray list = a.optJSONArray("emails");
        for (int i = 0; list != null && i < list.length() && emails.size() < limit; i++) {
            JSONObject o = list.optJSONObject(i);
            if (o == null || o.optString("uri").isEmpty()) continue;
            Email e = new Email();
            e.uri = o.optString("uri");
            e.date = o.optLong("date");
            e.read = o.optBoolean("read");
            e.starred = o.optBoolean("starred");
            e.attachments = o.optBoolean("attachments");
            e.messages = o.optInt("messages");
            e.subject = text(o, "subject");
            e.snippet = text(o, "snippet");
            e.from = text(o, "from");
            e.account = text(o, "account");
            e.type = text(o, "type");
            e.folder = text(o, "folder");
            emails.add(e);
        }
        return new Answer(emails, emails.isEmpty() ? text(a, "error") : null, a.optBoolean("server"));
    }

    private static String text(JSONObject o, String key) {
        return o.isNull(key) ? null : o.optString(key, null);
    }
}
