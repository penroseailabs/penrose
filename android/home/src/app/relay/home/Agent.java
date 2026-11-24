package app.relay.home;

import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.ShortcutInfo;
import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.net.wifi.WifiManager;
import android.os.BatteryManager;
import android.os.Build;
import android.provider.Settings;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/**
 * The agent's face: newline-delimited JSON on the abstract unix socket "relayhome", reached with
 * `adb forward tcp:0 localabstract:relayhome`, same dialect as the bridge. Only adb (shell) and root may
 * connect. Commands:
 *   {"cmd":"hello"}                          version, whether this app is the default home
 *   {"cmd":"search","q":"doordash orders"}   {"text": numbered lines, "results": [...], "mail": ...}, Gmail's emails
 *                                            included. "wait_ms": how long to wait for Gmail at most (15 s); what
 *                                            the phone itself has comes back then in any case, and "mail" says how
 *                                            it stands with Gmail: answered, phone_only, failed, pending or off.
 *                                            "retry": ask Gmail's servers again if they did not answer last time
 *   {"cmd":"mail","q":"doordash receipt"}    only the Gmail rows: the emails Gmail's server search found
 *   {"cmd":"act","n":2,"action":"open"}      runs an action of the last search's result n
 *   {"cmd":"apps"}                           every app with an icon: {"name", "package", "shortcuts": its entry
 *                                            points by label (Apps.isEntryPoint)}; no limit, nothing on screen
 *   {"cmd":"open","app":"DoorDash"}          search + open in one call (an app name or package ID);
 *                                            "shortcut":"Orders" opens that entry point of the app instead
 *   {"cmd":"context"}                        time, locale, battery, network: the phone at a glance
 *
 * act and open start an activity, which Android allows a launcher only while its screen shows (measured
 * 2026-11-22: with another app in front the start is dropped without an error, "Background activity launch
 * blocked"). So both refuse unless the home screen is in front: the caller brings it there first (HOME).
 */
final class Agent {
    static final String SOCKET = "relayhome";
    static final String VERSION = "0.5.0";
    private static final String NOT_SHOWING = "the home screen is not in front: Android lets the launcher open an "
        + "app or a shortcut only while it shows. Go home first, then ask again";
    private static final long MAIL_WAIT_MS = 15_000;
    private static final int SHELL_UID = 2000;

    private final Context context;
    private final Search search;
    private List<Result> last;

    Agent(Context context, Search search) {
        this.context = context;
        this.search = search;
    }

    void start() {
        Thread t = new Thread(this::listen, "relay-home-agent");
        t.setDaemon(true);
        t.start();
    }

    private void listen() {
        try (LocalServerSocket server = new LocalServerSocket(SOCKET)) {
            while (true) {
                LocalSocket socket = server.accept();
                Thread t = new Thread(() -> serve(socket), "relay-home-conn");
                t.setDaemon(true);
                t.start();
            }
        } catch (Throwable t) {
            android.util.Log.e("relay-home", "socket " + SOCKET + " failed: " + t);
        }
    }

    private void serve(LocalSocket socket) {
        try (LocalSocket s = socket) {
            int uid = s.getPeerCredentials().getUid();
            if (uid != 0 && uid != SHELL_UID) return;
            BufferedReader in = new BufferedReader(new InputStreamReader(s.getInputStream(), StandardCharsets.UTF_8));
            Writer out = new OutputStreamWriter(s.getOutputStream(), StandardCharsets.UTF_8);
            String line;
            while ((line = in.readLine()) != null) {
                JSONObject response;
                Object id = null;
                try {
                    JSONObject request = new JSONObject(line);
                    id = request.opt("id");
                    response = handle(request);
                } catch (Throwable t) {
                    response = new JSONObject().put("ok", false).put("error", t.getClass().getSimpleName() + ": " + t.getMessage());
                }
                if (id != null) response.put("id", id);
                out.write(response.toString());
                out.write('\n');
                out.flush();
            }
        } catch (Throwable ignored) {
        }
    }

    JSONObject handle(JSONObject r) throws Exception {
        String cmd = r.getString("cmd");
        switch (cmd) {
            case "hello":
                return ok().put("version", VERSION).put("pkg", context.getPackageName()).put("sdk", Build.VERSION.SDK_INT)
                    .put("default_home", isDefaultHome()).put("apps", search.apps.apps().size());
            case "search":
            case "mail": {
                String q = r.optString("q", "");
                // One answer, with Gmail's emails in it (Gmail searches its servers: about a second, several
                // when the phone has just been asleep or other searches are ahead in the queue). It never waits
                // longer than the caller says: the phone's own results go back then, and "mail" says Gmail's
                // are still to come. "mail": false answers at once without them.
                boolean mail = r.optBoolean("mail", true);
                if (mail && r.optBoolean("retry")) search.retryMail(q);
                if (mail && search.mailPending(q)) search.fetchMail(q, Math.max(0, r.optLong("wait_ms", MAIL_WAIT_MS)));
                List<Result> results = search.run(q);
                if (cmd.equals("mail")) results.removeIf(res -> !res.kind.equals(Result.MAIL));
                synchronized (this) { last = results; }
                JSONArray arr = new JSONArray();
                for (int i = 0; i < results.size(); i++) arr.put(results.get(i).json(i + 1));
                return ok().put("text", Search.lines(results)).put("results", arr).put("mail", mail ? search.mailState(q) : Mail.OFF);
            }
            case "act": {
                List<Result> results;
                synchronized (this) { results = last; }
                int n = r.optInt("n", 0);
                if (results == null) return fail("search first: act needs a result number from the last search");
                if (n < 1 || n > results.size()) return fail("no result [" + n + "]: the last search had " + results.size());
                Result res = results.get(n - 1);
                String action = r.optString("action", res.actions.get(0));
                if (!HomeActivity.showing) return fail(NOT_SHOWING);
                String outcome = Actions.perform(context, res, action);
                boolean done = !outcome.startsWith("refused") && !outcome.startsWith("failed");
                return new JSONObject().put("ok", done).put("result", outcome);
            }
            case "apps": {
                Map<String, List<ShortcutInfo>> entries = search.apps.entryPoints(null);
                JSONArray arr = new JSONArray();
                Set<String> seen = new HashSet<>();  // an app with two icons is one app
                for (Apps.AppEntry a : search.apps.apps()) {
                    if (!seen.add(a.pkg)) continue;
                    JSONArray shortcuts = new JSONArray();
                    List<ShortcutInfo> list = entries.get(a.pkg);
                    if (list != null) for (ShortcutInfo si : list) shortcuts.put(Apps.label(si));
                    arr.put(new JSONObject().put("name", a.label).put("package", a.pkg).put("shortcuts", shortcuts));
                }
                return ok().put("apps", arr);
            }
            case "open": {
                String app = r.optString("app", "");
                if (app.isEmpty()) return fail("open needs app: a name or a package ID");
                Apps.AppEntry exact = search.apps.byPackage(app);
                Result target = null;
                if (exact != null) target = new Result(Result.APP, exact.label, exact.pkg, 0, exact, "open", "info");
                else for (Result res : search.run(app)) if (res.kind.equals(Result.APP)) { target = res; break; }
                if (target == null) return fail("no installed app matches \"" + app + "\"");
                String shortcut = r.optString("shortcut", "");
                if (!shortcut.isEmpty()) {
                    // Named by app and label, never by a result number: searches from other connections (a
                    // look-up while a step works) would move the numbers under the caller.
                    Apps.AppEntry entry = (Apps.AppEntry) target.payload;
                    List<ShortcutInfo> list = search.apps.entryPoints(entry.pkg).get(entry.pkg);
                    if (list == null) list = new ArrayList<>();
                    ShortcutInfo si = Apps.pick(list, shortcut);
                    if (si == null) {
                        List<String> labels = new ArrayList<>();
                        for (ShortcutInfo each : list) labels.add(Apps.label(each));
                        return fail(entry.label + " has no shortcut \"" + shortcut + "\"; " + (labels.isEmpty()
                            ? "it offers none now" : "it has: " + String.join(" | ", labels))).put("shortcuts", new JSONArray(labels));
                    }
                    target = new Result(Result.SHORTCUT, Apps.label(si), entry.label, 0, si, "open");
                }
                if (!HomeActivity.showing) return fail(NOT_SHOWING);
                String outcome = Actions.perform(context, target, "open");
                return new JSONObject().put("ok", !outcome.startsWith("failed")).put("result", outcome);
            }
            case "context":
                return ok().put("text", context());
            default:
                return fail("unknown cmd " + cmd + ": hello, search, mail, act, apps, open, context");
        }
    }

    /** The phone at a glance, as labelled lines. */
    String context() {
        StringBuilder b = new StringBuilder();
        b.append("time: ").append(new SimpleDateFormat("EEE d MMM yyyy HH:mm zzz", Locale.ENGLISH).format(new Date())).append('\n');
        b.append("locale: ").append(Locale.getDefault().toLanguageTag()).append('\n');
        Intent battery = context.registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        if (battery != null) {
            int level = battery.getIntExtra(BatteryManager.EXTRA_LEVEL, -1), scale = battery.getIntExtra(BatteryManager.EXTRA_SCALE, 100);
            boolean charging = battery.getIntExtra(BatteryManager.EXTRA_STATUS, -1) == BatteryManager.BATTERY_STATUS_CHARGING;
            b.append("battery: ").append(level * 100 / Math.max(scale, 1)).append('%').append(charging ? " charging" : "").append('\n');
        }
        WifiManager wifi = (WifiManager) context.getApplicationContext().getSystemService(Context.WIFI_SERVICE);
        b.append("wifi: ").append(wifi != null && wifi.isWifiEnabled() ? "on" : "off").append('\n');
        b.append("airplane: ").append(Settings.Global.getInt(context.getContentResolver(), Settings.Global.AIRPLANE_MODE_ON, 0) != 0 ? "on" : "off").append('\n');
        b.append("home app: ").append(isDefaultHome() ? "penrose (this app)" : "another launcher").append('\n');
        b.append("apps installed: ").append(search.apps.apps().size());
        return b.toString();
    }

    boolean isDefaultHome() {
        Intent home = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME);
        android.content.pm.ResolveInfo ri = context.getPackageManager().resolveActivity(home, android.content.pm.PackageManager.MATCH_DEFAULT_ONLY);
        return ri != null && ri.activityInfo != null && context.getPackageName().equals(ri.activityInfo.packageName);
    }

    private static JSONObject ok() throws Exception { return new JSONObject().put("ok", true); }

    private static JSONObject fail(String why) throws Exception { return new JSONObject().put("ok", false).put("error", why); }
}
