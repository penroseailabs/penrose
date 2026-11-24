package app.relay.bridge;

import android.app.Activity;
import android.os.SystemClock;

import org.json.JSONObject;

/**
 * "Has the screen stopped changing?", asked right after an action instead of sleeping a fixed time.
 *
 * The signal is layout, not drawing: every window of this process passes through ViewRootImpl.performLayout
 * when something changes size or position (a new screen, a dialog, a list filling in, the keyboard), while a
 * spinner, a video, a shimmer placeholder or a blinking cursor only redraws. So a screen that animates forever
 * still settles; one that keeps re-laying itself out (a ticker) runs to the cap.
 *
 * An activity start or finish in this process (a tap that opens another screen or app, Back) is the other
 * signal: between the call and the new window appearing the old screen is perfectly quiet, so the wait holds
 * until this activity loses window focus or another one resumes, within the cap.
 *
 * "Has anything changed since then?" (change) is the other question, asked by a wait for something to appear
 * (wait_for): it blocks until the next layout or activity change, so the host reads the screen again only when
 * there is something new to read.
 */
final class Settle {
    static volatile long lastLayout;  // SystemClock.uptimeMillis()
    static volatile long lastLaunch;
    /** An activity start or finish this long before the wait began still counts: the action that caused it
     * was the one just sent. */
    private static final long LAUNCH_GRACE_MS = 2000;

    private static final Object CHANGED = new Object();

    static void layout() {
        lastLayout = SystemClock.uptimeMillis();
        synchronized (CHANGED) { CHANGED.notifyAll(); }
    }

    static void launch() {
        lastLaunch = SystemClock.uptimeMillis();
        synchronized (CHANGED) { CHANGED.notifyAll(); }
    }

    /** Waits until this process lays out or starts or finishes an activity after `since` (uptime ms, from a
     * snapshot's "uptime"), at most maxMs. changed=false: nothing changed within the cap. */
    static JSONObject change(long since, long maxMs) throws Exception {
        long start = SystemClock.uptimeMillis();
        synchronized (CHANGED) {
            while (true) {
                long now = SystemClock.uptimeMillis();
                if (lastLayout > since || lastLaunch > since) {
                    return new JSONObject().put("changed", true).put("waited_ms", now - start).put("uptime", now);
                }
                long left = maxMs - (now - start);
                if (left <= 0) return new JSONObject().put("changed", false).put("waited_ms", now - start).put("uptime", now);
                CHANGED.wait(left);
            }
        }
    }

    /** Waits until no layout for quietMs and no activity change pending, or maxMs. settled=false says the
     * screen was still changing at the cap. fromCall: the quiet time counts from the call too, so the first frame
     * of an action sent just before is never missed; without it a screen quiet for quietMs already is settled. */
    static JSONObject await(long quietMs, long maxMs, boolean fromCall) throws Exception {
        long start = SystemClock.uptimeMillis();
        Activity before = Dump.currentActivity();
        while (true) {
            long now = SystemClock.uptimeMillis();
            boolean launching = lastLaunch >= start - LAUNCH_GRACE_MS && before != null
                && before == Dump.currentActivity() && before.hasWindowFocus();
            long quiet = now - (fromCall ? Math.max(lastLayout, start) : lastLayout);
            if (!launching && quiet >= quietMs) return result(true, null, now - start, now);
            if (now - start >= maxMs) return result(false, launching ? "opening a screen" : "laying out", now - start, now);
            Thread.sleep(16);
        }
    }

    private static JSONObject result(boolean settled, String busy, long waited, long now) throws Exception {
        JSONObject out = new JSONObject().put("settled", settled).put("waited_ms", waited)
            .put("since_layout_ms", lastLayout == 0 ? JSONObject.NULL : now - lastLayout);
        if (busy != null) out.put("busy", busy);
        return out;
    }
}
