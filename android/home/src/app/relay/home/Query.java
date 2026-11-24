package app.relay.home;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/** A search string, normalised once, and the scoring every source uses. */
final class Query {
    final String raw;
    final String text;          // lower-case, punctuation folded to spaces, trimmed
    final List<String> tokens;  // the words of `text`

    Query(String raw) {
        this.raw = raw == null ? "" : raw.trim();
        this.text = normalise(this.raw);
        this.tokens = new ArrayList<>();
        for (String t : text.split(" ")) if (!t.isEmpty()) tokens.add(t);
    }

    boolean isEmpty() { return tokens.isEmpty(); }

    static String normalise(String s) {
        if (s == null) return "";
        StringBuilder b = new StringBuilder(s.length());
        boolean space = true;
        for (char c : s.toLowerCase(Locale.ROOT).toCharArray()) {
            if (Character.isLetterOrDigit(c)) { b.append(c); space = false; }
            else if (!space) { b.append(' '); space = true; }
        }
        return b.toString().trim();
    }

    /**
     * How well `candidate` answers this query, 0 when it does not. Every token must be found: as a
     * whole word (best), at the start of a word, anywhere, or as a subsequence of one word (a typo-tolerant
     * last resort: "wtsap" finds WhatsApp). An exact or prefix match of the whole text scores extra, so
     * "maps" ranks Google Maps above an app that merely mentions maps.
     */
    int score(String candidate) {
        if (isEmpty()) return 1;
        String c = normalise(candidate);
        if (c.isEmpty()) return 0;
        String[] words = c.split(" ");
        int total = 0;
        for (String t : tokens) {
            int best = 0;
            for (String w : words) {
                int s = w.equals(t) ? 40 : w.startsWith(t) ? 30 : w.contains(t) ? 18 : subsequence(w, t) ? 6 : 0;
                if (s > best) best = s;
            }
            if (best == 0) return 0;
            total += best;
        }
        if (c.equals(text)) total += 60;
        else if (c.startsWith(text)) total += 25;
        return total;
    }

    private static boolean subsequence(String word, String t) {
        if (t.length() < 3 || t.length() > word.length()) return false;
        int i = 0;
        for (char ch : word.toCharArray()) if (i < t.length() && ch == t.charAt(i)) i++;
        return i == t.length();
    }
}
