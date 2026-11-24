package app.relay.home;

import android.content.Context;
import android.graphics.Rect;
import android.os.Bundle;
import android.view.View;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityNodeInfo.AccessibilityAction;
import android.view.accessibility.AccessibilityNodeProvider;
import android.widget.ListView;

import java.util.Collections;
import java.util.List;

/**
 * The results list, readable whole. A ListView builds only the rows that fit on screen and the bridge reads
 * only what is on screen, so the agent would see 10 of 50 results and have to scroll for the rest. Instead
 * this list describes every result to accessibility as a virtual node (title, kind and subtitle, its actions,
 * its row number), marked visible because each one is a tap away: the agent's screen lists all of them, and
 * a tap or long press reaches any of them through the accessibility action, on screen or not. The rows draw
 * their own text (RowView), so the rows on screen are not listed a second time.
 */
final class ResultsList extends ListView {
    interface Listener {
        void run(Result r, String action);
        void choose(Result r);
    }

    /** Accessibility ids for a result's actions after the first (the first is the click). */
    private static final int[] MORE = {R.id.action_2, R.id.action_3, R.id.action_4, R.id.action_5, R.id.action_6};

    private List<Result> results = Collections.emptyList();
    private Listener listener;
    private Provider provider;

    ResultsList(Context context) {
        super(context);
    }

    /** `results` is read live: the activity changes it in place and calls notifyDataSetChanged. */
    void bind(List<Result> results, Listener listener) {
        this.results = results;
        this.listener = listener;
    }

    @Override
    public AccessibilityNodeProvider getAccessibilityNodeProvider() {
        if (provider == null) provider = new Provider();  // ListView's constructor asks before fields are set
        return provider;
    }

    /**
     * Every result is already in the agent's view (the virtual nodes below), so the list does not tell it there
     * is more to scroll to: the bridge reads this for its scroll hint. A finger still scrolls the list (AbsListView
     * scrolls by touch without asking this).
     */
    @Override
    public boolean canScrollVertically(int direction) {
        return false;
    }

    /**
     * Straight quotes as curly ones: the agent's screen puts each result's text in straight quotes, and a subject
     * such as `Ordered: "Earpads for…"` would end it early. Opening after a space or bracket, closing elsewhere.
     */
    static String curly(String s) {
        if (s.indexOf('"') < 0) return s;
        StringBuilder b = new StringBuilder(s.length());
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c != '"') { b.append(c); continue; }
            char before = i == 0 ? ' ' : s.charAt(i - 1);
            b.append(Character.isWhitespace(before) || "([{".indexOf(before) >= 0 ? '“' : '”');
        }
        return b.toString();
    }

    /** Where row i is on screen; a row scrolled out of view gets an empty box at the list's edge. */
    private Rect bounds(int i) {
        int[] at = new int[2];
        getLocationOnScreen(at);
        Rect list = new Rect(at[0], at[1], at[0] + getWidth(), at[1] + getHeight());
        View row = getChildAt(i - getFirstVisiblePosition());
        if (row != null) {
            int[] r = new int[2];
            row.getLocationOnScreen(r);
            Rect box = new Rect(r[0], r[1], r[0] + row.getWidth(), r[1] + row.getHeight());
            if (box.intersect(list)) return box;
        }
        int edge = i < getFirstVisiblePosition() ? list.top : list.bottom;
        return new Rect(list.left, edge, list.right, edge);
    }

    private final class Provider extends AccessibilityNodeProvider {
        @Override
        public AccessibilityNodeInfo createAccessibilityNodeInfo(int id) {
            ResultsList host = ResultsList.this;
            if (id == HOST_VIEW_ID) {
                // Not host.createAccessibilityNodeInfo(): that comes back here.
                AccessibilityNodeInfo info = AccessibilityNodeInfo.obtain(host);
                host.onInitializeAccessibilityNodeInfo(info);
                for (int i = 0; i < results.size(); i++) info.addChild(host, i);
                return info;
            }
            if (id < 0 || id >= results.size()) return null;
            Result r = results.get(id);
            AccessibilityNodeInfo info = AccessibilityNodeInfo.obtain(host, id);
            info.setPackageName(getContext().getPackageName());
            info.setClassName(Result.class.getName());  // the agent reads it as role "Result"
            info.setParent(host);
            // One text, not text + description: readers drop a title that appears inside the description
            // ("se" in "search Gmail").
            info.setText(curly(r.title + " · " + r.describe()));
            info.setBoundsInScreen(bounds(id));
            info.setVisibleToUser(true);
            info.setEnabled(true);
            info.setFocusable(true);
            info.setClickable(true);
            info.addAction(new AccessibilityAction(AccessibilityNodeInfo.ACTION_CLICK, r.actions.get(0)));
            if (r.actions.size() > 1) {
                // A long press offers every action, as it does for a finger; each one is also named here.
                info.setLongClickable(true);
                info.addAction(new AccessibilityAction(AccessibilityNodeInfo.ACTION_LONG_CLICK, null));
                for (int k = 1; k < r.actions.size() && k <= MORE.length; k++)
                    info.addAction(new AccessibilityAction(MORE[k - 1], r.actions.get(k)));
            }
            info.setCollectionItemInfo(AccessibilityNodeInfo.CollectionItemInfo.obtain(id, 1, 0, 1, false));
            return info;
        }

        @Override
        public boolean performAction(int id, int action, Bundle args) {
            if (id == HOST_VIEW_ID) return performAccessibilityAction(action, args);
            if (id < 0 || id >= results.size() || listener == null) return false;
            Result r = results.get(id);
            if (action == AccessibilityNodeInfo.ACTION_CLICK) {
                listener.run(r, r.actions.get(0));
                return true;
            }
            if (action == AccessibilityNodeInfo.ACTION_LONG_CLICK && r.actions.size() > 1) {
                listener.choose(r);
                return true;
            }
            for (int k = 1; k < r.actions.size() && k <= MORE.length; k++) {
                if (action == MORE[k - 1]) {
                    listener.run(r, r.actions.get(k));
                    return true;
                }
            }
            return false;
        }
    }
}
