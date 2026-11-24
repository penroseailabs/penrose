package app.relay.bridge;

import android.inputmethodservice.InputMethodService;
import android.view.View;
import android.view.inputmethod.InlineSuggestion;
import android.view.inputmethod.InlineSuggestionInfo;
import android.view.inputmethod.InlineSuggestionsResponse;
import android.widget.inline.InlineContentView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.lang.reflect.Method;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.WeakHashMap;
import java.util.function.Consumer;

import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedBridge;

/**
 * Autofill chips in the keyboard (Google Password Manager's saved sign-ins, in Gboard's suggestion
 * strip). Android hands them to the current keyboard as {@link InlineSuggestion}s, and the keyboard
 * inflates each into an {@link InlineContentView} whose content another process (Play services)
 * draws: no keyboard, hooked or not, can read the account name on a chip or pick one by code.
 * What the keyboard does know is recorded here: how many chips came, which are sign-ins and which
 * are actions (the Password Manager key that opens the full list), their autofill hints, and which
 * view shows which, so the dump lists each chip as an element and the host taps it on screen.
 * Installed in keyboard processes only (Module).
 */
final class Inline {
    /** InlineContentView -> {kind, index, of, hints, pinned, source} of the chip it shows. */
    static final Map<View, JSONObject> views = Collections.synchronizedMap(new WeakHashMap<>());
    private static final Map<InlineSuggestion, JSONObject> infos = Collections.synchronizedMap(new WeakHashMap<>());
    private static final Set<Class<?>> hooked = Collections.synchronizedSet(new HashSet<>());

    static void install(ClassLoader cl) throws Throwable {
        // The keyboard overrides onInlineSuggestionsResponse; the override is hooked once the
        // service exists, whatever the keyboard calls its class.
        Module.all(InputMethodService.class, "onCreate", Module.after(p -> hookResponse(p.thisObject.getClass())));
        Module.all(InputMethodService.class, "onInlineSuggestionsResponse", Module.before(p -> response(p.args[0])));
        Module.all(InlineSuggestion.class, "inflate", Module.before(Inline::inflating));
    }

    private static void hookResponse(Class<?> c) {
        for (; c != null && c != InputMethodService.class; c = c.getSuperclass()) {
            if (!hooked.add(c)) return;
            try {
                Method m = c.getDeclaredMethod("onInlineSuggestionsResponse", InlineSuggestionsResponse.class);
                XposedBridge.hookMethod(m, new XC_MethodHook() {
                    @Override protected void beforeHookedMethod(MethodHookParam p) {
                        try { response(p.args[0]); } catch (Throwable t) { Problems.record("hook_callback_error", "inline_response", t); }
                    }
                });
                return;
            } catch (NoSuchMethodException ignored) {
            }
        }
    }

    /** A new set of chips (an empty one when the field loses focus or Google has nothing for it). */
    static void response(Object arg) throws Exception {
        if (!(arg instanceof InlineSuggestionsResponse)) return;
        List<InlineSuggestion> list = ((InlineSuggestionsResponse) arg).getInlineSuggestions();
        int suggestions = 0;
        // An account Google must confirm first (a password saved for a related site) is typed an action, like the
        // pinned Password Manager key: the pin, not the type, tells an account from the key.
        for (InlineSuggestion s : list) if (!s.getInfo().isPinned()) suggestions++;
        JSONArray hints = new JSONArray();
        int index = 0;
        for (InlineSuggestion s : list) {
            InlineSuggestionInfo info = s.getInfo();
            boolean suggestion = !info.isPinned();
            JSONObject d = new JSONObject();
            d.put("kind", suggestion ? "suggestion" : "action");
            if (suggestion) d.put("index", ++index).put("of", suggestions);
            d.put("pinned", info.isPinned());
            d.put("type", String.valueOf(info.getType()).replace("android:", ""));
            d.put("source", String.valueOf(info.getSource()).replace("android:", ""));
            JSONArray h = new JSONArray();
            if (info.getAutofillHints() != null) {
                for (String hint : info.getAutofillHints()) { h.put(hint); if (suggestion) hints.put(hint); }
            }
            d.put("hints", h);
            infos.put(s, d);
        }
        if (!list.isEmpty()) {
            Events.emit("inline_suggestions", "suggestions", suggestions, "actions", list.size() - suggestions, "hints", hints);
        }
    }

    /** inflate(Context, Size, Executor, Consumer<InlineContentView>): the view, once made, is tied to its chip. */
    private static void inflating(XC_MethodHook.MethodHookParam p) throws Exception {
        if (!(p.thisObject instanceof InlineSuggestion) || p.args.length < 4 || !(p.args[3] instanceof Consumer)) return;
        InlineSuggestion s = (InlineSuggestion) p.thisObject;
        JSONObject info = infos.get(s);
        if (info == null) {  // inflated without a response seen (the hook came late): still a chip
            info = new JSONObject().put("kind", s.getInfo().isPinned() ? "action" : "suggestion")
                .put("pinned", s.getInfo().isPinned()).put("type", String.valueOf(s.getInfo().getType()).replace("android:", ""));
        }
        final JSONObject chip = info;
        @SuppressWarnings("unchecked")
        final Consumer<InlineContentView> original = (Consumer<InlineContentView>) p.args[3];
        p.args[3] = (Consumer<InlineContentView>) v -> {
            if (v != null) views.put(v, chip);
            original.accept(v);
        };
    }
}
