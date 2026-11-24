package app.relay.home;

import java.util.List;

/**
 * The query as a search elsewhere, always last: Google in Chrome, Maps, YouTube and Play Store. When no
 * installed app or shortcut answered, a Play result says so with the install action instead of leaving
 * the agent with "not installed".
 */
final class Web implements Source {
    @Override
    public void search(Query q, List<Result> out) {
        if (q.isEmpty()) return;
        // Something on the phone answered (an app, a shortcut, a settings page, a contact): no Play fallback.
        // The Gmail search row is there for every query, so it does not count.
        boolean found = false;
        for (Result r : out) if (!r.kind.equals(Result.ACTIVITY) && !r.kind.equals(Result.MAIL)) { found = true; break; }
        if (!found)
            out.add(new Result(Result.PLAY, q.raw, "not installed: search and install from Play Store", 2, q.raw, "install"));
        out.add(new Result(Result.WEB, q.raw, "", 1, q.raw, "google", "maps", "youtube", "play"));
    }
}
