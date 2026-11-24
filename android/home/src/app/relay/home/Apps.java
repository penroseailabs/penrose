package app.relay.home;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ActivityInfo;
import android.content.pm.LauncherApps;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.content.pm.ShortcutInfo;
import android.os.Process;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * Installed apps, their published shortcuts (static, dynamic and pinned: what Nova and Sesame search),
 * and every activity in their manifests, scanned on install rather than learned by use. A shortcut or an
 * activity matches on its own label and its app's name together, so "whatsapp jordan" and "jordan" both find
 * the conversation. Activities are a scan: an exported one opens from here; an internal one needs the
 * bridge inside the app (or root) and says so.
 */
final class Apps implements Source {
    static final class AppEntry {
        final String label, pkg;
        final ComponentName launch;
        AppEntry(String label, String pkg, ComponentName launch) { this.label = label; this.pkg = pkg; this.launch = launch; }
    }

    static final class ActivityEntry {
        final AppEntry app;
        final ComponentName component;
        final String name;      // humanised class name: CartActivity -> Cart
        final boolean exported;
        ActivityEntry(AppEntry app, ComponentName component, String name, boolean exported) {
            this.app = app; this.component = component; this.name = name; this.exported = exported;
        }
    }

    private final Context context;
    private List<AppEntry> apps;
    private List<ActivityEntry> activities;

    Apps(Context context) { this.context = context; }

    /** Forget the scan: a package was added, changed or removed. */
    synchronized void invalidate() { apps = null; activities = null; }

    synchronized List<AppEntry> apps() {
        if (apps != null) return apps;
        PackageManager pm = context.getPackageManager();
        Intent main = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
        List<AppEntry> out = new ArrayList<>();
        for (ResolveInfo r : pm.queryIntentActivities(main, 0)) {
            ActivityInfo a = r.activityInfo;
            if (a.packageName.equals(context.getPackageName())) continue;
            out.add(new AppEntry(String.valueOf(r.loadLabel(pm)), a.packageName, new ComponentName(a.packageName, a.name)));
        }
        Collections.sort(out, (x, y) -> x.label.compareToIgnoreCase(y.label));
        return apps = out;
    }

    synchronized List<ActivityEntry> activities() {
        if (activities != null) return activities;
        PackageManager pm = context.getPackageManager();
        List<ActivityEntry> out = new ArrayList<>();
        for (AppEntry app : apps()) {
            PackageInfo info;
            try {
                info = pm.getPackageInfo(app.pkg, PackageManager.GET_ACTIVITIES | PackageManager.MATCH_DISABLED_COMPONENTS);
            } catch (Exception e) {
                continue;
            }
            if (info.activities == null) continue;
            for (ActivityInfo a : info.activities) {
                String name = humanise(a.name);
                if (name == null || a.name.equals(app.launch.getClassName())) continue;
                out.add(new ActivityEntry(app, new ComponentName(a.packageName, a.name), name, a.exported));
            }
        }
        return activities = out;
    }

    /** `com.x.ui.OrderHistoryActivity` becomes "Order History"; null for names that are not a screen of their own. */
    static String humanise(String className) {
        String simple = className.substring(className.lastIndexOf('.') + 1);
        if (simple.contains("$") || simple.isEmpty()) return null;
        for (String junk : new String[]{"Alias", "Proxy", "Trampoline", "Stub", "Receiver", "Service", "Provider"})
            if (simple.endsWith(junk)) return null;
        for (String suffix : new String[]{"Activity", "Screen", "Page"})
            if (simple.endsWith(suffix) && simple.length() > suffix.length()) simple = simple.substring(0, simple.length() - suffix.length());
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < simple.length(); i++) {
            char c = simple.charAt(i);
            if (i > 0 && Character.isUpperCase(c) && !Character.isUpperCase(simple.charAt(i - 1))) b.append(' ');
            b.append(c);
        }
        return b.toString().trim();
    }

    AppEntry byPackage(String pkg) {
        for (AppEntry a : apps()) if (a.pkg.equals(pkg)) return a;
        return null;
    }

    @Override
    public void search(Query q, List<Result> out) {
        for (AppEntry a : apps()) {
            String id = a.pkg.substring(a.pkg.lastIndexOf('.') + 1);
            int s = Math.max(q.score(a.label), q.score(id) - 5);
            if (s > 0) out.add(new Result(Result.APP, a.label, a.pkg, s + 20, a, "open", "info"));
        }
        if (q.isEmpty()) return;
        shortcuts(q, out);
        // Only exported screens until the bridge can open internal ones (phase 2): a result the agent
        // cannot act on is noise. Dedupe by app and name (Play Core ships the same wrappers in every app).
        java.util.Set<String> seen = new java.util.HashSet<>();
        List<Result> screens = new ArrayList<>();
        for (ActivityEntry e : activities()) {
            if (!e.exported) continue;
            int s = q.score(e.app.label + " " + e.name);
            if (s == 0) continue;
            // A lone word is an app name or a screen name, not both: "doordash" lists the app, not its 80
            // screens, and "cart" lists every app's cart only when no app is called that.
            if (q.tokens.size() == 1 && (q.score(e.app.label) > 0 || q.score(e.name) == 0)) continue;
            if (!seen.add(e.app.pkg + "/" + e.name)) continue;
            screens.add(new Result(Result.ACTIVITY, e.name, e.app.label, s - 15, e, "open"));
        }
        Collections.sort(screens, (a, b) -> b.score - a.score);
        out.addAll(screens.size() > MAX_SCREENS ? screens.subList(0, MAX_SCREENS) : screens);
    }

    static final int MAX_SCREENS = 6;
    static final int MAX_ENTRY_POINTS = 10;  // per app; Android shows four or five of an app's shortcuts

    /** Every published shortcut (static, dynamic, pinned) of `pkg`, or of every app when it is null; empty when
     *  this app is not the home screen (only the launcher may read them). */
    private List<ShortcutInfo> published(String pkg) {
        LauncherApps la = (LauncherApps) context.getSystemService(Context.LAUNCHER_APPS_SERVICE);
        if (la == null || !la.hasShortcutHostPermission()) return Collections.emptyList();
        LauncherApps.ShortcutQuery sq = new LauncherApps.ShortcutQuery().setQueryFlags(
            LauncherApps.ShortcutQuery.FLAG_MATCH_DYNAMIC | LauncherApps.ShortcutQuery.FLAG_MATCH_MANIFEST
                | LauncherApps.ShortcutQuery.FLAG_MATCH_PINNED);
        if (pkg != null) sq.setPackage(pkg);
        try {
            List<ShortcutInfo> infos = la.getShortcuts(sq, Process.myUserHandle());
            return infos == null ? Collections.<ShortcutInfo>emptyList() : infos;
        } catch (Exception e) {
            return Collections.emptyList();
        }
    }

    /**
     * Whether a shortcut is a fixed way into its app (Search, Orders, New timer) and not a person, a chat or an
     * account: those come and go, and are the user's data, not the app's map. A shortcut the app declares in its
     * manifest is one. A dynamic one is when its icon is one of the app's own resources and it is not long-lived:
     * conversations are long-lived (WhatsApp, Teams, Outlook), and people and accounts carry a picture, which is a
     * bitmap (Phone, Contacts, Gmail's account). Measured on a phone with 93 shortcuts (2026-11-22): 52 kept, all
     * navigation; 28 people, chats and accounts left out, and four navigation ones drawn as bitmaps.
     */
    static boolean isEntryPoint(ShortcutInfo si) {
        if (!si.isEnabled() || label(si) == null) return false;
        if (si.isDeclaredInManifest()) return true;
        if (!si.isDynamic()) return false;
        String flags = flags(si);
        return flags.contains("Ic-r") && !flags.contains("Liv");
    }

    /**
     * A shortcut's flags as Android prints them (`flags=0x1a4 [ImManIc-rStr]`): Ic-r an icon from the app's
     * resources, Liv long-lived. The public API has neither. When a version of Android prints them differently
     * this is empty, and only the manifest's shortcuts count as entry points: fewer, never a person.
     */
    static String flags(ShortcutInfo si) {
        String s = String.valueOf(si);
        int at = s.indexOf("flags=");
        int open = at < 0 ? -1 : s.indexOf('[', at);
        int close = open < 0 ? -1 : s.indexOf(']', open);
        return close < 0 ? "" : s.substring(open + 1, close);
    }

    static String label(ShortcutInfo si) {
        CharSequence label = si.getShortLabel() != null ? si.getShortLabel() : si.getLongLabel();
        return label == null || label.toString().trim().isEmpty() ? null : label.toString().trim();
    }

    /**
     * The entry points of `pkg` (of every app when it is null), by package: the manifest's first, then by the
     * app's own order, one per label (YouTube publishes each of its three four times), MAX_ENTRY_POINTS at most.
     * An app's dynamic ones exist only once it has run.
     */
    java.util.Map<String, List<ShortcutInfo>> entryPoints(String pkg) {
        List<ShortcutInfo> kept = new ArrayList<>();
        for (ShortcutInfo si : published(pkg)) if (isEntryPoint(si)) kept.add(si);
        Collections.sort(kept, (a, b) -> a.isDeclaredInManifest() != b.isDeclaredInManifest()
            ? (a.isDeclaredInManifest() ? -1 : 1) : a.getRank() - b.getRank());
        java.util.Map<String, List<ShortcutInfo>> out = new java.util.HashMap<>();
        java.util.Set<String> seen = new java.util.HashSet<>();
        for (ShortcutInfo si : kept) {
            List<ShortcutInfo> list = out.get(si.getPackage());
            if (list == null) out.put(si.getPackage(), list = new ArrayList<>());
            if (list.size() < MAX_ENTRY_POINTS && seen.add(si.getPackage() + "\n" + Query.normalise(label(si)))) list.add(si);
        }
        return out;
    }

    /**
     * The one of `entries` that `wanted` names: its short or long label, whatever the case and punctuation
     * ("wi-fi" is "Wi‑Fi"); else the only one whose label holds those words ("orders" is "My Orders"). Null when
     * none or several do.
     */
    static ShortcutInfo pick(List<ShortcutInfo> entries, String wanted) {
        String want = Query.normalise(wanted);
        if (want.isEmpty()) return null;
        ShortcutInfo held = null;
        int holding = 0;
        for (ShortcutInfo si : entries) {
            String shortLabel = Query.normalise(String.valueOf(si.getShortLabel()));
            String longLabel = si.getLongLabel() == null ? "" : Query.normalise(si.getLongLabel().toString());
            if (shortLabel.equals(want) || longLabel.equals(want)) return si;
            if ((" " + shortLabel + " ").contains(" " + want + " ") || (" " + longLabel + " ").contains(" " + want + " ")) {
                held = si;
                holding++;
            }
        }
        return holding == 1 ? held : null;
    }

    private void shortcuts(Query q, List<Result> out) {
        List<ShortcutInfo> infos = published(null);
        for (ShortcutInfo si : infos) {
            if (!si.isEnabled()) continue;
            AppEntry app = byPackage(si.getPackage());
            String appLabel = app != null ? app.label : si.getPackage();
            CharSequence label = si.getShortLabel() != null ? si.getShortLabel() : si.getLongLabel();
            if (label == null) continue;
            int s = q.score(appLabel + " " + label);
            if (s == 0) continue;
            // "whatsapp" alone is the app; a shortcut needs its own label hit, or a second word.
            if (q.tokens.size() == 1 && q.score(String.valueOf(label)) == 0) continue;
            out.add(new Result(Result.SHORTCUT, String.valueOf(label), appLabel, s + 10, si, "open"));
        }
    }
}
