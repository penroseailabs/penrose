package app.relay.home;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * The search engine both faces share: runs every source for a query and ranks the hits. One instance
 * per process; the app scan is cached until a package changes.
 */
final class Search {
    static final int LIMIT = 50;

    final Apps apps;
    final Mail mail;
    private final List<Source> sources = new ArrayList<>();

    Search(Context context) {
        apps = new Apps(context);
        mail = new Mail(context);
        sources.add(apps);
        sources.add(new Contacts(context));
        sources.add(new SettingsPages());
        sources.add(mail);
        sources.add(new Web());  // last: it looks at what the others found
        IntentFilter f = new IntentFilter();
        f.addAction(Intent.ACTION_PACKAGE_ADDED);
        f.addAction(Intent.ACTION_PACKAGE_REMOVED);
        f.addAction(Intent.ACTION_PACKAGE_CHANGED);
        f.addAction(Intent.ACTION_PACKAGE_REPLACED);
        f.addDataScheme("package");
        context.registerReceiver(new BroadcastReceiver() {
            @Override
            public void onReceive(Context c, Intent i) { apps.invalidate(); }
        }, f);
    }

    /** Whether Gmail's emails for `raw` are still to come (fetchMail gets them; run shows them after). */
    boolean mailPending(String raw) { return mail.pending(new Query(raw)); }

    /** Start Gmail's search helper while `raw` is typed, so fetchMail pays no start-up. */
    void warmMail(String raw) { mail.warm(new Query(raw)); }

    /** Gmail's search for `raw`: one to two seconds, more after the phone slept; back after `waitMs` at the latest. */
    void fetchMail(String raw, long waitMs) { mail.fetch(new Query(raw), waitMs); }

    /** Ask Gmail's servers again for `raw` when they did not answer the last time (Mail.retry). */
    void retryMail(String raw) { mail.retry(new Query(raw)); }

    /** How it stands with Gmail for `raw` (Mail.state). */
    String mailState(String raw) { return mail.state(new Query(raw)); }

    /** The ranked hits for `raw`, at most LIMIT; the whole app list, alphabetical, for an empty query. */
    List<Result> run(String raw) {
        Query q = new Query(raw);
        List<Result> out = new ArrayList<>();
        for (Source s : sources) {
            try {
                s.search(q, out);
            } catch (Exception ignored) {
                // One broken source must not take the search down.
            }
        }
        if (q.isEmpty()) return out;
        Collections.sort(out, (a, b) -> b.score != a.score ? b.score - a.score
            : a.order != b.order ? a.order - b.order : a.title.compareToIgnoreCase(b.title));
        if (out.size() <= LIMIT) return out;
        // The searches elsewhere (Gmail, web, Play) rank last but stay: the cap cuts the phone's own hits.
        List<Result> elsewhere = new ArrayList<>(), kept = new ArrayList<>();
        for (Result r : out) (elsewhere(r) ? elsewhere : kept).add(r);
        kept = new ArrayList<>(kept.subList(0, Math.min(kept.size(), LIMIT - elsewhere.size())));
        kept.addAll(elsewhere);
        return kept;
    }

    private static boolean elsewhere(Result r) {
        return r.kind.equals(Result.MAIL) || r.kind.equals(Result.WEB) || r.kind.equals(Result.PLAY);
    }

    /** The results as the agent reads them, one line each, numbered from 1. */
    static String lines(List<Result> results) {
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < results.size(); i++) {
            if (i > 0) b.append('\n');
            b.append(results.get(i).line(i + 1));
        }
        return b.toString();
    }
}
