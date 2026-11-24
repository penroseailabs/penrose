package app.relay.bridge;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.regex.Pattern;

/**
 * The hooked app's SharedPreferences, through the app's own in-memory instances (the same objects
 * its code holds), so a change takes effect in the running app and its change listeners fire.
 * Editing the XML file as root while the app runs does neither: the app keeps its cached copy
 * and writes it back over the edit.
 *
 * Values under keys that look like credentials are never returned (they would reach a model);
 * they can still be overwritten or removed.
 */
final class Prefs {
    private static final int MAX_ENTRIES = 200;
    private static final Pattern SECRET = Pattern.compile(
        "(?i)(token|secret|passw|passcode|credential|api[_-]?key|private[_-]?key|bearer|cookie|session|otp|auth|\\bpin\\b)");
    static final String REDACTED = "[redacted]";

    private static JSONObject ok() throws Exception { return new JSONObject().put("ok", true); }
    private static JSONObject fail(String why) throws Exception { return new JSONObject().put("ok", false).put("error", why); }

    private static Context app() throws Exception {
        return (Context) Class.forName("android.app.ActivityThread").getMethod("currentApplication").invoke(null);
    }

    private static File dir(Context c) {
        return new File(c.getApplicationInfo().dataDir, "shared_prefs");
    }

    static JSONObject list() throws Exception {
        String[] files = dir(app()).list((d, n) -> n.endsWith(".xml"));
        JSONArray names = new JSONArray();
        if (files != null) {
            Arrays.sort(files);
            for (String f : files) names.put(f.substring(0, f.length() - 4));
        }
        return ok().put("pkg", Module.pkg).put("files", names);
    }

    private static String typeOf(Object v) {
        if (v instanceof Boolean) return "bool";
        if (v instanceof Integer) return "int";
        if (v instanceof Long) return "long";
        if (v instanceof Float) return "float";
        if (v instanceof Set) return "string_set";
        return "string";
    }

    private static Object show(String key, Object v) {
        if (SECRET.matcher(key).find()) return REDACTED;
        if (v instanceof Set) return new JSONArray((Set<?>) v);
        if (v instanceof String) return Events.clip((String) v, 500);
        return v;
    }

    /** One key, or (key null) every entry of the file, capped. */
    static JSONObject get(String name, String key) throws Exception {
        Context c = app();
        if (!new File(dir(c), name + ".xml").exists()) return ok().put("exists", false).put("name", name);
        SharedPreferences sp = c.getSharedPreferences(name, Context.MODE_PRIVATE);
        Map<String, ?> all = sp.getAll();
        if (key != null) {
            if (!all.containsKey(key)) return ok().put("name", name).put("key", key).put("present", false);
            Object v = all.get(key);
            return ok().put("name", name).put("key", key).put("present", true).put("type", typeOf(v)).put("value", show(key, v));
        }
        JSONObject out = new JSONObject();
        int n = 0;
        for (Map.Entry<String, ?> e : new TreeMap<String, Object>(all).entrySet()) {
            if (n++ >= MAX_ENTRIES) break;
            out.put(e.getKey(), new JSONObject().put("type", typeOf(e.getValue())).put("value", show(e.getKey(), e.getValue())));
        }
        return ok().put("name", name).put("exists", true).put("count", all.size()).put("entries", out)
            .put("truncated", all.size() > MAX_ENTRIES);
    }

    /** Writes one value. `type` (bool, int, long, float, string, string_set) is inferred from the
     * existing value when omitted, so only a new key needs it. */
    static JSONObject set(String name, String key, Object value, String type) throws Exception {
        Context c = app();
        SharedPreferences sp = c.getSharedPreferences(name, Context.MODE_PRIVATE);
        Object old = sp.getAll().get(key);
        if (type == null) {
            if (old == null) return fail("the key does not exist yet: give a type (bool, int, long, float, string, string_set)");
            type = typeOf(old);
        }
        SharedPreferences.Editor ed = sp.edit();
        String text = String.valueOf(value);
        switch (type) {
            case "bool": ed.putBoolean(key, value instanceof Boolean ? (Boolean) value : Boolean.parseBoolean(text)); break;
            case "int": ed.putInt(key, Integer.parseInt(text)); break;
            case "long": ed.putLong(key, Long.parseLong(text)); break;
            case "float": ed.putFloat(key, Float.parseFloat(text)); break;
            case "string": ed.putString(key, text); break;
            case "string_set": {
                JSONArray a = value instanceof JSONArray ? (JSONArray) value : new JSONArray(text);
                Set<String> set = new HashSet<>();
                for (int i = 0; i < a.length(); i++) set.add(a.getString(i));
                ed.putStringSet(key, set);
                break;
            }
            default: return fail("unknown type " + type);
        }
        boolean saved = ed.commit();
        Object now = sp.getAll().get(key);
        return ok().put("ok", saved).put("name", name).put("key", key).put("type", type)
            .put("old", old == null ? JSONObject.NULL : show(key, old)).put("new", show(key, now))
            .put("note", "the running app reads this through its own SharedPreferences, so it is live; a screen already drawn may need to reopen to show it");
    }

    static JSONObject remove(String name, String key) throws Exception {
        SharedPreferences sp = app().getSharedPreferences(name, Context.MODE_PRIVATE);
        boolean had = sp.contains(key);
        boolean saved = sp.edit().remove(key).commit();
        return ok().put("ok", saved).put("name", name).put("key", key).put("was_present", had);
    }
}
