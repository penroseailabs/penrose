package app.relay.bridge;

import android.os.SystemClock;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Things the module could not do the normal way inside this app: a hook callback that threw,
 * a reflective path that is missing, a node provider that returned nothing. Deduplicated by
 * kind + where + exception class, counted, and read by the host with the `diag` command, so
 * each app teaches which fallback the module needs.
 */
final class Problems {
    private static final int CAPACITY = 200;
    private static final Map<String, JSONObject> problems = new LinkedHashMap<>();

    static synchronized void record(String kind, String where, Throwable t) {
        record(kind, where, t == null ? null : t.getClass().getName() + ": " + Events.clip(String.valueOf(t.getMessage()), 300),
            t == null || t.getStackTrace().length == 0 ? null : t.getStackTrace()[0].toString());
    }

    static synchronized void record(String kind, String where, String detail, String at) {
        String key = kind + "|" + where + "|" + (detail == null ? "" : detail.split(":", 2)[0]);
        try {
            JSONObject p = problems.get(key);
            long now = SystemClock.uptimeMillis();
            if (p == null) {
                if (problems.size() >= CAPACITY) return;
                p = new JSONObject().put("kind", kind).put("where", where).put("count", 0).put("first", now);
                if (detail != null) p.put("detail", detail);
                if (at != null) p.put("at", at);
                problems.put(key, p);
            }
            p.put("count", p.getInt("count") + 1).put("last", now);
        } catch (Exception ignored) {
        }
    }

    static synchronized JSONArray all() {
        JSONArray out = new JSONArray();
        for (JSONObject p : problems.values()) out.put(p);
        return out;
    }
}
