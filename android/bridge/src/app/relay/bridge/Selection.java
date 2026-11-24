package app.relay.bridge;

import android.graphics.Color;
import android.graphics.Rect;
import android.view.View;
import android.view.ViewGroup;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Which cell of a row of Compose cells is the selected one, when the app draws each cell's text
 * with Android TextViews hosted in the Compose view (AndroidView interop) and the cells' semantics
 * carry no selected state. One cinema app's date strip is like this: seven clickable virtual nodes, no
 * selected flag anywhere, and the chosen date's texts are white on a filled cell while the others
 * are dark or grey.
 *
 * Evidence, strongest first: a hosted view inside the cell that is selected or activated; else
 * the colour of the cell's most prominent text (largest size) when exactly one cell in a row of
 * three or more has a colour no other cell has, and its text is inverted against the rest (far
 * lighter or far darker than every other cell's). A row where the
 * semantics already mark a cell is left alone.
 */
final class Selection {
    private static final int MIN_CELLS = 3;
    /** Relative luminance (0 black .. 1 white) the odd colour must be apart from every other one:
     * a selected cell inverts its text (white on a filled cell), while status colours (green,
     * orange, red showtimes) sit close together and must not read as a selection. */
    private static final double MIN_LUMINANCE_GAP = 0.4;
    private static final int MAX_LABELS = 400;

    private static final class Label {
        final int cx, cy, colour;
        final float size;
        final boolean selected;
        Label(Rect r, int colour, float size, boolean selected) {
            this.cx = r.centerX();
            this.cy = r.centerY();
            this.colour = colour;
            this.size = size;
            this.selected = selected;
        }
    }

    /** `virtual`: the host's virtual nodes as Dump described them; flags are added in place. */
    static void infer(View host, int[] off, List<JSONObject> virtual) {
        if (!(host instanceof ViewGroup) || virtual.isEmpty()) return;
        List<Label> labels = new ArrayList<>();
        collect((ViewGroup) host, off, false, labels);
        if (labels.isEmpty()) return;
        try {
            for (JSONObject n : virtual) visit(n, labels);
        } catch (Exception e) {
            Problems.record("selection", host.getClass().getName(), e);
        }
    }

    private static void collect(ViewGroup g, int[] off, boolean selectedAbove, List<Label> out) {
        for (int i = 0; i < g.getChildCount() && out.size() < MAX_LABELS; i++) {
            View c = g.getChildAt(i);
            if (c.getVisibility() != View.VISIBLE || c.getAlpha() <= 0.01f) continue;
            boolean selected = selectedAbove || (c.isSelected() && !Dump.isMarquee(c)) || c.isActivated();
            if (c instanceof TextView && ((TextView) c).length() > 0) {
                Rect r = new Rect();
                if (c.getGlobalVisibleRect(r) && !r.isEmpty()) {
                    r.offset(off[0], off[1]);
                    TextView tv = (TextView) c;
                    out.add(new Label(r, tv.getCurrentTextColor(), tv.getTextSize(), selected));
                }
            }
            if (c instanceof ViewGroup) collect((ViewGroup) c, off, selected, out);
        }
    }

    private static void visit(JSONObject n, List<Label> labels) throws Exception {
        JSONArray kids = n.optJSONArray("c");
        if (kids == null) return;
        List<JSONObject> cells = new ArrayList<>();
        boolean marked = false;
        for (int i = 0; i < kids.length(); i++) {
            JSONObject k = kids.getJSONObject(i);
            if (has(k, "click")) cells.add(k);
            if (has(k, "selected")) marked = true;
        }
        if (!marked && cells.size() >= 2) row(cells, labels);
        for (int i = 0; i < kids.length(); i++) visit(kids.getJSONObject(i), labels);
    }

    private static void row(List<JSONObject> cells, List<Label> labels) throws Exception {
        List<Label> prominent = new ArrayList<>();
        boolean direct = false;
        for (JSONObject cell : cells) {
            JSONArray b = cell.optJSONArray("b");
            Label best = null;
            boolean selected = false;
            if (b != null) {
                Rect r = new Rect(b.getInt(0), b.getInt(1), b.getInt(2), b.getInt(3));
                for (Label l : labels) {
                    if (!r.contains(l.cx, l.cy)) continue;
                    if (l.selected) selected = true;
                    if (best == null || l.size > best.size) best = l;
                }
            }
            if (selected) {
                Dump.flag(cell, "selected");
                cell.put("selected_by", "hosted view");
                direct = true;
            }
            prominent.add(best);
        }
        if (direct || cells.size() < MIN_CELLS || prominent.contains(null)) return;
        Map<Integer, Integer> uses = new HashMap<>();
        for (Label l : prominent) uses.merge(l.colour, 1, Integer::sum);
        int odd = -1;
        for (int i = 0; i < prominent.size(); i++) {
            if (uses.get(prominent.get(i).colour) != 1) continue;
            if (odd >= 0) return;  // two or more one-off colours: a colourful row, not a selection
            odd = i;
        }
        if (odd < 0 || uses.size() < 2) return;
        int colour = prominent.get(odd).colour;
        for (int other : uses.keySet()) {
            if (other != colour && Math.abs(luminance(colour) - luminance(other)) < MIN_LUMINANCE_GAP) return;
        }
        Dump.flag(cells.get(odd), "selected");
        cells.get(odd).put("selected_by", "text colour");
    }

    /** WCAG relative luminance of an sRGB colour. */
    private static double luminance(int c) {
        return 0.2126 * linear(Color.red(c)) + 0.7152 * linear(Color.green(c)) + 0.0722 * linear(Color.blue(c));
    }

    private static double linear(int channel) {
        double v = channel / 255.0;
        return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    }

    private static boolean has(JSONObject n, String flag) {
        JSONArray f = n.optJSONArray("f");
        if (f == null) return false;
        for (int i = 0; i < f.length(); i++) if (flag.equals(f.optString(i))) return true;
        return false;
    }
}
