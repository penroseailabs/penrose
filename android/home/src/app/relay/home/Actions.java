package app.relay.home;

import android.app.SearchManager;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.pm.LauncherApps;
import android.content.pm.ShortcutInfo;
import android.net.Uri;
import android.os.Process;
import android.provider.ContactsContract;
import android.provider.Settings;

/** Runs one action of a result and says in a line what it did: the agent's "last result". */
final class Actions {
    static final String CHROME = "com.android.chrome";

    static String perform(Context ctx, Result r, String action) {
        if (!r.actions.contains(action)) return "refused: " + r.kind + " \"" + r.title + "\" accepts " + String.join(" | ", r.actions);
        try {
            switch (r.kind) {
                case Result.APP: {
                    Apps.AppEntry a = (Apps.AppEntry) r.payload;
                    if (action.equals("info"))
                        return start(ctx, new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + a.pkg)), "app info of " + a.label);
                    Intent launch = ctx.getPackageManager().getLaunchIntentForPackage(a.pkg);
                    if (launch == null) launch = new Intent(Intent.ACTION_MAIN).setComponent(a.launch);
                    return start(ctx, launch, "opened " + a.label + " (" + a.pkg + ")");
                }
                case Result.SHORTCUT: {
                    ShortcutInfo si = (ShortcutInfo) r.payload;
                    LauncherApps la = (LauncherApps) ctx.getSystemService(Context.LAUNCHER_APPS_SERVICE);
                    la.startShortcut(si.getPackage(), si.getId(), null, null, Process.myUserHandle());
                    return "opened shortcut \"" + r.title + "\" of " + r.subtitle;
                }
                case Result.ACTIVITY: {
                    Apps.ActivityEntry e = (Apps.ActivityEntry) r.payload;
                    Intent i = new Intent(Intent.ACTION_MAIN).setComponent(e.component);
                    try {
                        return start(ctx, i, "opened " + e.app.label + " > " + e.name);
                    } catch (SecurityException se) {
                        return "refused: " + e.app.label + " > " + e.name + " is internal to the app; open it through the bridge "
                            + "(" + e.component.flattenToShortString() + ") or navigate inside the app";
                    }
                }
                case Result.CONTACT: {
                    Contacts.Entry c = (Contacts.Entry) r.payload;
                    switch (action) {
                        case "call":
                            return start(ctx, new Intent(Intent.ACTION_CALL, Uri.parse("tel:" + c.phone)), "calling " + c.name + " " + c.phone);
                        case "sms":
                            return start(ctx, new Intent(Intent.ACTION_SENDTO, Uri.parse("smsto:" + c.phone)), "SMS to " + c.name + " " + c.phone);
                        case "whatsapp":
                            return start(ctx, new Intent(Intent.ACTION_VIEW, Uri.parse("https://wa.me/" + digits(c.phone))).setPackage("com.whatsapp"),
                                "WhatsApp chat with " + c.name);
                        case "email":
                            return start(ctx, new Intent(Intent.ACTION_SENDTO, Uri.parse("mailto:" + c.email)), "email to " + c.name + " " + c.email);
                        default:
                            return start(ctx, new Intent(Intent.ACTION_VIEW, Uri.withAppendedPath(ContactsContract.Contacts.CONTENT_FILTER_URI, Uri.encode(c.name))),
                                "showing contact " + c.name);
                    }
                }
                case Result.SETTING:
                    return start(ctx, new Intent((String) r.payload), "opened Settings > " + r.title);
                case Result.MAIL: {
                    if (r.payload instanceof Gmail.Email) return openEmail(ctx, (Gmail.Email) r.payload);
                    return gmailSearch(ctx, (String) r.payload);
                }
                case Result.PLAY:
                    return start(ctx, new Intent(Intent.ACTION_VIEW, Uri.parse("market://search?q=" + Uri.encode((String) r.payload))),
                        "Play Store search for \"" + r.payload + "\": install from there");
                case Result.WEB: {
                    String q = Uri.encode((String) r.payload);
                    switch (action) {
                        case "maps":
                            return start(ctx, new Intent(Intent.ACTION_VIEW, Uri.parse("geo:0,0?q=" + q)), "Maps search for \"" + r.payload + "\"");
                        case "youtube":
                            return start(ctx, new Intent(Intent.ACTION_VIEW, Uri.parse("https://www.youtube.com/results?search_query=" + q)),
                                "YouTube search for \"" + r.payload + "\"");
                        case "play":
                            return start(ctx, new Intent(Intent.ACTION_VIEW, Uri.parse("market://search?q=" + q)), "Play Store search for \"" + r.payload + "\"");
                        default: {
                            Intent i = new Intent(Intent.ACTION_VIEW, Uri.parse("https://www.google.com/search?q=" + q));
                            if (installed(ctx, CHROME)) i.setPackage(CHROME);
                            return start(ctx, i, "Google search for \"" + r.payload + "\" in Chrome");
                        }
                    }
                }
                default:
                    return "refused: unknown kind " + r.kind;
            }
        } catch (ActivityNotFoundException e) {
            return "failed: no app handles " + action + " for " + r.kind + " \"" + r.title + "\"";
        } catch (SecurityException e) {
            return "failed: not allowed: " + e.getMessage();
        }
    }

    /** Gmail's own search intent: Gmail opens on its results for the query. */
    private static String gmailSearch(Context ctx, String query) {
        return start(ctx, new Intent(Intent.ACTION_SEARCH).setPackage(Mail.GMAIL).putExtra(SearchManager.QUERY, query),
            "Gmail search for \"" + query + "\"");
    }

    /**
     * One email in Gmail: the mail contract's conversation link with the account's mail type, the way
     * Gmail's widget opens one. If Gmail does not take it, its search for the subject and sender.
     */
    private static String openEmail(Context ctx, Gmail.Email e) {
        String what = "email \"" + (e.subject == null ? "" : e.subject) + "\" in Gmail";
        if (e.uri != null && e.type != null) {
            Intent view = new Intent(Intent.ACTION_VIEW).setDataAndType(Uri.parse(e.uri), e.type).setPackage(Mail.GMAIL);
            if (e.folder != null) view.putExtra("folderUri", Uri.parse(e.folder));
            try {
                return start(ctx, view, "opened " + what);
            } catch (ActivityNotFoundException | SecurityException ignored) {
                // Gmail changed how it opens conversations: fall back to its search.
            }
        }
        String from = e.from != null && e.from.contains("<") ? e.from.substring(e.from.indexOf('<') + 1).replace(">", "").trim() : e.from;
        String q = (e.subject != null && !e.subject.isEmpty() ? "subject:(" + e.subject + ")" : "")
            + (from != null && !from.isEmpty() ? " from:" + from : "");
        return gmailSearch(ctx, q.trim()).replace("Gmail search for", "Gmail search for " + what + ":");
    }

    private static String start(Context ctx, Intent i, String did) {
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        ctx.startActivity(i);
        return did;
    }

    static boolean installed(Context ctx, String pkg) {
        try {
            ctx.getPackageManager().getPackageInfo(pkg, 0);
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    private static String digits(String phone) {
        StringBuilder b = new StringBuilder();
        for (char c : phone.toCharArray()) if (Character.isDigit(c)) b.append(c);
        return b.toString();
    }
}
