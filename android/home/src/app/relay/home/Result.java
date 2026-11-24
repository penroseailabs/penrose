package app.relay.home;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.Arrays;
import java.util.List;

/**
 * One search hit, in the shape the agent reads: a kind, a title, what tells it apart, and the actions
 * it accepts. The same object draws a row for the user. `payload` is what the action needs (a
 * component, a shortcut, a phone number); it never reaches the agent.
 */
final class Result {
    static final String APP = "App", SHORTCUT = "Shortcut", ACTIVITY = "Activity", CONTACT = "Contact",
        SETTING = "Setting", MAIL = "Mail", WEB = "Web", PLAY = "Play";

    final String kind;
    final String title;
    final String subtitle;   // the package, the owning app, a phone number: what tells look-alikes apart
    final List<String> actions;
    final Object payload;
    int score;
    int order;  // among equal scores, before the title: keeps emails newest first
    Result(String kind, String title, String subtitle, int score, Object payload, String... actions) {
        this.kind = kind;
        this.title = title;
        this.subtitle = subtitle == null ? "" : subtitle;
        this.score = score;
        this.payload = payload;
        this.actions = Arrays.asList(actions);
    }

    /** `[3] Shortcut "Orders" DoorDash {open}`: the line the agent reads. */
    String line(int n) {
        StringBuilder b = new StringBuilder();
        b.append('[').append(n).append("] ").append(kind).append(" \"").append(ResultsList.curly(title)).append('"');
        if (!subtitle.isEmpty()) b.append(' ').append(subtitle);
        b.append(" {").append(String.join(" | ", actions)).append('}');
        return b.toString();
    }

    /** `Shortcut DoorDash`: the kind and what tells it apart, as the row's second line and the agent's screen show it. */
    String describe() {
        return subtitle.isEmpty() ? kind : kind + " " + subtitle;
    }

    JSONObject json(int n) throws Exception {
        return new JSONObject().put("n", n).put("kind", kind).put("title", title).put("subtitle", subtitle)
            .put("actions", new JSONArray(actions));
    }
}
