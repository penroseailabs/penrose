package app.relay.bridge;

import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;

/**
 * Which sign-ins Google Password Manager has saved, by site and account name only: never a password or a key.
 * Relay tells its agent "passkeys for x, passwords for y are saved here" so it uses Chrome's or Google's own offer
 * instead of asking the user for a password.
 *
 * Run as root through app_process with this APK as the class path (the gateway does it), it prints one JSON line:
 *
 *   {"ok":true,"passwords":[{"realm":"https://www.amazon.in/","user":"a@b.com"}],
 *    "passkeys":[{"rp":"amazon.in","user":"a@b.com"}], "apps":{"in.amazon.mShop.android.shopping":["https://www.amazon.in"]},
 *    "app_groups":{...the same, by brand group}}
 *
 * The source is Google Play services' own index of the account's synced data (chromesync.data_store), opened
 * read-only: password_index (site, username), webauthn_credential_index (site) with the passkey's user name
 * from its sync record (WebauthnCredentialSpecifics: rp_id = 3, user_name = 7, user_display_name = 8), and the
 * app-to-website groups Google uses to offer a site's passwords in its app (facet_group_index, affiliation_index).
 */
public final class SavedSignins {
    static final String STORE = "/data/data/com.google.android.gms/databases/chromesync.data_store";
    private static final int MAX_DEPTH = 5;

    public static void main(String[] args) {
        PrintStream out = new PrintStream(new FileOutputStream(FileDescriptor.out), true);
        try (SQLiteDatabase db = SQLiteDatabase.openDatabase(STORE, null,
                SQLiteDatabase.OPEN_READONLY | SQLiteDatabase.NO_LOCALIZED_COLLATORS)) {
            JSONObject answer = new JSONObject().put("ok", true);
            answer.put("passwords", passwords(db));
            answer.put("passkeys", passkeys(db));
            answer.put("apps", apps(db, "affiliation_index"));
            answer.put("app_groups", apps(db, "facet_group_index"));
            out.println(answer);
        } catch (Throwable t) {
            out.println("{\"ok\":false,\"error\":" + JSONObject.quote(t.getClass().getSimpleName() + ": " + t.getMessage()) + "}");
            System.exit(1);
        }
        System.exit(0);
    }

    private static JSONArray passwords(SQLiteDatabase db) throws Exception {
        JSONArray found = new JSONArray();
        Set<String> seen = new LinkedHashSet<>();
        try (Cursor c = db.rawQuery("SELECT p.idx_signon_realm, p.idx_username FROM password_index p "
                + "LEFT JOIN sync_entities s ON s.account = p.account AND s.id = p.id "
                + "WHERE p.idx_username != '' AND IFNULL(s.is_deleted_locally, 0) = 0", null)) {
            while (c.moveToNext()) {
                String realm = c.getString(0), user = c.getString(1);
                if (realm != null && user != null && seen.add(realm + "\n" + user)) {
                    found.put(new JSONObject().put("realm", realm).put("user", user));
                }
            }
        }
        return found;
    }

    private static JSONArray passkeys(SQLiteDatabase db) throws Exception {
        JSONArray found = new JSONArray();
        Set<String> seen = new LinkedHashSet<>();
        try (Cursor c = db.rawQuery("SELECT w.idx_rp_id, s.value FROM webauthn_credential_index w "
                + "LEFT JOIN sync_entities s ON s.account = w.account AND s.id = w.id "
                + "WHERE IFNULL(s.is_deleted_locally, 0) = 0", null)) {
            while (c.moveToNext()) {
                String rp = c.getString(0);
                byte[] value = c.isNull(1) ? null : c.getBlob(1);
                String user = value == null ? null : userName(value, rp, 0);
                if (rp != null && seen.add(rp + "\n" + user)) {
                    JSONObject key = new JSONObject().put("rp", rp);
                    if (user != null) key.put("user", user);
                    found.put(key);
                }
            }
        }
        return found;
    }

    /** package -> the websites `table` links to its android:// facet: affiliation_index holds the sites that are the
     * same service as the app (Google offers their passwords in it), facet_group_index the wider brand group. */
    private static JSONObject apps(SQLiteDatabase db, String table) throws Exception {
        Map<String, Set<String>> sites = new HashMap<>();
        {
            try (Cursor c = db.rawQuery("SELECT a.facet_id, w.facet_id FROM " + table + " a JOIN " + table
                    + " w ON w.id = a.id WHERE a.facet_id LIKE 'android://%' AND w.facet_id LIKE 'https://%'", null)) {
                while (c.moveToNext()) {
                    String facet = c.getString(0);
                    int at = facet.lastIndexOf('@');
                    if (at < 0) continue;
                    sites.computeIfAbsent(facet.substring(at + 1), k -> new LinkedHashSet<>()).add(c.getString(1));
                }
            }
        }
        JSONObject apps = new JSONObject();
        for (Map.Entry<String, Set<String>> e : sites.entrySet()) apps.put(e.getKey(), new JSONArray(e.getValue()));
        return apps;
    }

    /** The user name in the protobuf message (at any depth) whose field 3 is `rp`: field 7, else field 8. */
    static String userName(byte[] b, String rp, int depth) {
        String rpField = null, name = null, display = null;
        java.util.List<byte[]> nested = new java.util.ArrayList<>();
        int i = 0;
        try {
            while (i < b.length) {
                long key = 0;
                int shift = 0;
                while (true) {
                    int x = b[i++] & 0xff;
                    key |= (long) (x & 0x7f) << shift;
                    shift += 7;
                    if (x < 0x80) break;
                }
                int field = (int) (key >>> 3), wire = (int) (key & 7);
                if (wire == 0) {
                    while ((b[i++] & 0x80) != 0) { /* varint */ }
                } else if (wire == 1) {
                    i += 8;
                } else if (wire == 5) {
                    i += 4;
                } else if (wire == 2) {
                    long n = 0;
                    shift = 0;
                    while (true) {
                        int x = b[i++] & 0xff;
                        n |= (long) (x & 0x7f) << shift;
                        shift += 7;
                        if (x < 0x80) break;
                    }
                    if (n < 0 || i + n > b.length) return null;
                    byte[] sub = java.util.Arrays.copyOfRange(b, i, i + (int) n);
                    i += (int) n;
                    if (field == 3) rpField = new String(sub, StandardCharsets.UTF_8);
                    else if (field == 7) name = new String(sub, StandardCharsets.UTF_8);
                    else if (field == 8) display = new String(sub, StandardCharsets.UTF_8);
                    if (n > 1) nested.add(sub);
                } else {
                    return null;  // not a protobuf message
                }
            }
        } catch (ArrayIndexOutOfBoundsException e) {
            return null;
        }
        if (i != b.length) return null;
        if (rp.equals(rpField)) {
            if (name != null && !name.isEmpty()) return name;
            if (display != null && !display.isEmpty()) return display;
            return null;
        }
        if (depth >= MAX_DEPTH) return null;
        for (byte[] sub : nested) {
            String found = userName(sub, rp, depth + 1);
            if (found != null) return found;
        }
        return null;
    }
}
