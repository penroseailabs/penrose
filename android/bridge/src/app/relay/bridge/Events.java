package app.relay.bridge;

import android.content.Intent;
import android.os.Bundle;
import android.os.SystemClock;

import org.json.JSONArray;
import org.json.JSONObject;

import java.lang.reflect.Method;
import java.util.ArrayDeque;

/**
 * Transient things the screen alone cannot show reliably: toasts, dialogs, errors, intents and
 * HTTP calls. Each event gets a sequence number; the host asks for everything after the last
 * number it saw, so a two-second toast is never missed between polls.
 */
final class Events {
    private static final int CAPACITY = 400;
    private static final int BODY_LIMIT = 4000;
    private static final ArrayDeque<JSONObject> events = new ArrayDeque<>();
    private static final ArrayDeque<JSONObject> network = new ArrayDeque<>();
    private static long seq;
    private static long errorWindowStart;
    private static int errorsInWindow;

    static void emit(String type, Object... keyValues) {
        try {
            JSONObject e = new JSONObject();
            e.put("type", type);
            e.put("t", SystemClock.uptimeMillis());
            for (int i = 0; i + 1 < keyValues.length; i += 2) {
                e.put((String) keyValues[i], keyValues[i + 1] == null ? JSONObject.NULL : keyValues[i + 1]);
            }
            push(events, e);
        } catch (Throwable ignored) {
        }
    }

    /** An event whose fields are already put together (a notification, as Notifier describes one). */
    static void emit(String type, JSONObject fields) {
        try {
            fields.put("type", type);
            fields.put("t", SystemClock.uptimeMillis());
            push(events, fields);
        } catch (Throwable ignored) {
        }
    }

    private static synchronized void push(ArrayDeque<JSONObject> queue, JSONObject e) throws Exception {
        e.put("seq", ++seq);
        queue.addLast(e);
        while (queue.size() > CAPACITY) queue.removeFirst();
        Events.class.notifyAll();
    }

    /**
     * The events after `after`, waiting up to `waitMs` for the first one: a read the host holds open, so it
     * hears of an event as it happens instead of asking over and over. Returns at once when the host's number is
     * not this process's last one (events it has not read, or a process that started again and counts from 1).
     */
    static synchronized JSONArray await(long after, long waitMs) throws InterruptedException {
        long deadline = SystemClock.elapsedRealtime() + waitMs;
        while (seq == after) {
            long left = deadline - SystemClock.elapsedRealtime();
            if (left <= 0) break;
            Events.class.wait(left);
        }
        return since(after, false);
    }

    static synchronized long lastSeq() { return seq; }

    static synchronized JSONArray since(long after, boolean fromNetwork) {
        JSONArray out = new JSONArray();
        for (JSONObject e : fromNetwork ? network : events) {
            if (e.optLong("seq") > after) out.put(e);
        }
        return out;
    }

    /** Log.e can be a firehose in some apps: keep at most 20 per 5 seconds. */
    static void logError(String tag, String message, Throwable t) {
        long now = SystemClock.uptimeMillis();
        synchronized (Events.class) {
            if (now - errorWindowStart > 5000) { errorWindowStart = now; errorsInWindow = 0; }
            if (++errorsInWindow > 20) return;
        }
        emit("log_error", "tag", tag, "message", clip(message, 300),
            "exception", t == null ? null : t.getClass().getName() + ": " + clip(t.getMessage(), 200));
    }

    static void intent(Intent i) {
        JSONArray extras = new JSONArray();
        Bundle b = i.getExtras();
        if (b != null) for (String k : b.keySet()) extras.put(k);
        emit("intent", "action", i.getAction(),
            "component", i.getComponent() == null ? null : i.getComponent().flattenToShortString(),
            "data", i.getDataString(), "extras", extras);
    }

    /** One OkHttp exchange. The summary goes to events; a body preview goes to the network log. */
    static void http(Object request, Object response, long ms, Throwable failure) {
        try {
            String method = String.valueOf(invoke(request, "method"));
            String url = String.valueOf(invoke(request, "url"));
            Object code = response == null ? null : invoke(response, "code");
            emit("http", "method", method, "url", clip(url, 300), "code", code, "ms", ms,
                "error", failure == null ? null : failure.getClass().getSimpleName() + ": " + clip(failure.getMessage(), 200));
            if (response == null) return;
            String type = String.valueOf(response.getClass().getMethod("header", String.class).invoke(response, "Content-Type"));
            String body = null;
            // Not a stream: peekBody blocks until BODY_LIMIT bytes or the end arrive, which for server-sent events,
            // NDJSON feeds or gRPC is when the app next gets data, so a chat or live screen froze until then.
            if ((type.contains("json") || type.contains("text") || type.contains("xml")) && !streaming(type)) {
                Object peek = response.getClass().getMethod("peekBody", long.class).invoke(response, (long) BODY_LIMIT);
                body = String.valueOf(invoke(peek, "string"));
            }
            JSONObject e = new JSONObject();
            e.put("method", method).put("url", url).put("code", code).put("ms", ms)
                .put("content_type", type).put("body", body == null ? JSONObject.NULL : body);
            push(network, e);
        } catch (Throwable ignored) {
        }
    }

    static boolean streaming(String type) {
        String t = type.toLowerCase(java.util.Locale.ROOT);
        return t.contains("event-stream") || t.contains("ndjson") || t.contains("stream+json") || t.contains("grpc")
            || t.contains("multipart") || t.contains("x-mixed-replace");
    }

    private static Object invoke(Object target, String name) throws Exception {
        Method m = target.getClass().getMethod(name);
        m.setAccessible(true);
        return m.invoke(target);
    }

    static String clip(String s, int limit) {
        if (s == null) return null;
        return s.length() <= limit ? s : s.substring(0, limit) + "…";
    }
}
