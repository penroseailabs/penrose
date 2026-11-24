package app.relay.bridge;

import android.app.Activity;
import android.app.Dialog;
import android.content.Context;
import android.content.Intent;
import android.os.SystemClock;
import android.view.View;
import android.view.WindowManager;
import android.widget.PopupWindow;
import android.widget.Toast;

import java.lang.ref.WeakReference;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.WeakHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;

import de.robv.android.xposed.IXposedHookLoadPackage;
import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedBridge;
import de.robv.android.xposed.callbacks.XC_LoadPackage;

/**
 * Entry point. Installs framework-level hooks only (Toast, Dialog, WindowManagerGlobal, OkHttp...), so the module works across apps and survives app updates. Everything the
 * hooks learn is kept here and read by {@link Server} on request; formatting for the model
 * happens on the host, so the APK rarely needs rebuilding.
 */
public class Module implements IXposedHookLoadPackage {
    static final String VERSION = "0.5.21";

    static String pkg = "";
    static String process = "";
    /** Hook name -> "ok" or the reason it could not be installed. Reported by `hello`. */
    static final Map<String, String> hooks = Collections.synchronizedMap(new LinkedHashMap<>());

    static volatile WeakReference<Activity> resumed = new WeakReference<>(null);
    static final Map<Object, Boolean> resumedFragments = Collections.synchronizedMap(new WeakHashMap<>());
    /** Every window root added in this process, with its layout params, in z-order of addition. */
    static final List<WeakReference<View>> roots = new CopyOnWriteArrayList<>();
    static final Map<View, WindowManager.LayoutParams> rootParams = Collections.synchronizedMap(new WeakHashMap<>());
    /** Drawable instance -> resource id it was loaded from (the drawable forgets it after loading). */
    static final Map<Object, Integer> drawableIds = Collections.synchronizedMap(new WeakHashMap<>());
    /** ImageView -> resource id given to setImageResource. */
    static final Map<View, Integer> viewIcons = Collections.synchronizedMap(new WeakHashMap<>());
    static final Map<Toast, CharSequence> toastText = Collections.synchronizedMap(new WeakHashMap<>());


    interface Body { void run() throws Throwable; }

    /** How often each hook's callback ran. A hook that installed but never fires (an AOT
     * app reaching the method another way) shows up as 0 here; the host flags it. */
    static final Map<String, AtomicInteger> fired = new java.util.concurrent.ConcurrentHashMap<>();
    /** The hook whose callbacks are being created, so after()/before() can attribute them. */
    private static String installing = "?";

    static void install(String name, Body body) {
        installing = name;
        fired.putIfAbsent(name, new AtomicInteger());
        try {
            body.run();
            hooks.put(name, "ok");
        } catch (Throwable t) {
            hooks.put(name, t.getClass().getSimpleName() + ": " + t.getMessage());
        }
    }

    static void all(Class<?> c, String method, XC_MethodHook callback) {
        if (XposedBridge.hookAllMethods(c, method, callback).isEmpty()) {
            throw new NoSuchMethodError(c.getName() + "." + method);
        }
    }

    static XC_MethodHook after(Body0 body) {
        final String hook = installing;
        return new XC_MethodHook() {
            @Override protected void afterHookedMethod(MethodHookParam p) {
                fired.get(hook).incrementAndGet();
                try { body.run(p); } catch (Throwable t) { Problems.record("hook_callback_error", hook, t); }
            }
        };
    }

    static XC_MethodHook before(Body0 body) {
        final String hook = installing;
        return new XC_MethodHook() {
            @Override protected void beforeHookedMethod(MethodHookParam p) {
                fired.get(hook).incrementAndGet();
                try { body.run(p); } catch (Throwable t) { Problems.record("hook_callback_error", hook, t); }
            }
        };
    }

    interface Body0 { void run(XC_MethodHook.MethodHookParam p) throws Throwable; }

    @Override
    public void handleLoadPackage(XC_LoadPackage.LoadPackageParam lp) {
        if (!lp.isFirstApplication) return;
        pkg = lp.packageName;
        process = lp.processName;
        ClassLoader cl = lp.classLoader;

        // SystemUI only enters the screen-lock PIN; the agents never read or drive it.
        if (pkg.equals("com.android.systemui")) {
            Unlocker.install(cl);
            return;
        }

        // Meet turns its call audio off on emulators: the apps that carry it see a real phone (DeviceProfile).
        if (DeviceProfile.wanted(pkg)) install("device_profile", DeviceProfile::install);

        // The keyboard shows Google Password Manager's saved sign-ins as chips drawn by another process:
        // the hook records which chip is which (Inline), the dump lists them, the host taps them.
        if (pkg.contains("inputmethod") || pkg.contains("keyboard")) install("inline", () -> Inline.install(cl));

        // What the phone refuses whoever asks (Gmail: deleting email), as a safeguard against prompt injection.
        if (Guard.active()) install("guard", Guard::install);

        install("windows", () -> all(Class.forName("android.view.WindowManagerGlobal"), "addView", after(p -> {
            if (!(p.args[0] instanceof View)) return;
            View v = (View) p.args[0];
            roots.add(new WeakReference<>(v));
            if (p.args[1] instanceof WindowManager.LayoutParams) rootParams.put(v, (WindowManager.LayoutParams) p.args[1]);
            roots.removeIf(r -> r.get() == null);
        })));

        // What Settle waits on: a layout pass in any window of this process, and an activity start or finish.
        install("settle", () -> {
            all(Class.forName("android.view.ViewRootImpl"), "performLayout", after(p -> Settle.layout()));
            all(android.app.Instrumentation.class, "execStartActivity", before(p -> Settle.launch()));
            all(Activity.class, "finish", before(p -> Settle.launch()));
        });

        // The framework's dispatch point first: AOT-compiled apps (Play Store) can reach
        // Activity.onResume without passing through a hook placed on it.
        install("activity", () -> all(android.app.Instrumentation.class, "callActivityOnResume",
            after(p -> onResumed((Activity) p.args[0]))));
        install("activity_onresume", () -> all(Activity.class, "onResume", after(p -> onResumed((Activity) p.thisObject))));

        install("fragments", () -> {
            Class<?> f = Class.forName("androidx.fragment.app.Fragment", false, cl);
            all(f, "onResume", after(p -> {
                resumedFragments.put(p.thisObject, true);
                Events.emit("fragment_resumed", "fragment", p.thisObject.getClass().getName());
            }));
            all(f, "onPause", after(p -> resumedFragments.remove(p.thisObject)));
        });

        install("drawables", () -> {
            all(Class.forName("android.content.res.ResourcesImpl"), "loadDrawable", after(p -> {
                Object d = p.getResult();
                if (d == null) return;
                // loadDrawable(Resources wrapper, TypedValue value, int id, int density, Theme theme)
                for (Object a : p.args) if (a instanceof Integer) { drawableIds.put(d, (Integer) a); return; }
            }));
            all(android.widget.ImageView.class, "setImageResource",
                after(p -> viewIcons.put((View) p.thisObject, (Integer) p.args[0])));
        });

        install("toast", () -> {
            all(Toast.class, "makeText", after(p -> {
                Context ctx = null;
                CharSequence text = null;
                for (Object a : p.args) {
                    if (a instanceof Context) ctx = (Context) a;
                    else if (a instanceof CharSequence) text = (CharSequence) a;
                }
                if (text == null && ctx != null) {
                    for (int i = 1; i < p.args.length - 1; i++) {
                        if (p.args[i] instanceof Integer) { text = ctx.getText((Integer) p.args[i]); break; }
                    }
                }
                if (p.getResult() instanceof Toast && text != null) toastText.put((Toast) p.getResult(), text);
            }));
            all(Toast.class, "setText", after(p -> {
                Toast t = (Toast) p.thisObject;
                Object a = p.args[0];
                if (a instanceof CharSequence) toastText.put(t, (CharSequence) a);
            }));
            all(Toast.class, "show", before(p -> {
                Toast t = (Toast) p.thisObject;
                CharSequence text = toastText.get(t);
                String shown = text != null ? text.toString() : Dump.joinTexts(t.getView(), 3);
                Events.emit("toast", "text", shown);
            }));
        });

        install("dialog", () -> {
            all(Dialog.class, "show", after(p -> {
                Dialog d = (Dialog) p.thisObject;
                if (d.getClass().getName().equals("android.inputmethodservice.SoftInputWindow")) return;  // the keyboard's own window (a hidden class)
                View decor = d.getWindow() != null ? d.getWindow().getDecorView() : null;
                Events.emit("dialog_shown", "class", d.getClass().getName(), "text", Dump.joinTexts(decor, 8));
            }));
            all(Dialog.class, "dismiss", after(p -> {
                if (p.thisObject.getClass().getName().equals("android.inputmethodservice.SoftInputWindow")) return;
                Events.emit("dialog_dismissed", "class", p.thisObject.getClass().getName());
            }));
        });

        install("popup", () -> {
            XC_MethodHook shown = after(p -> Events.emit("popup_shown",
                "text", Dump.joinTexts(((PopupWindow) p.thisObject).getContentView(), 8)));
            all(PopupWindow.class, "showAtLocation", shown);
            all(PopupWindow.class, "showAsDropDown", shown);
        });

        install("snackbar", () -> all(
            Class.forName("com.google.android.material.snackbar.BaseTransientBottomBar", false, cl), "show",
            after(p -> {
                Object view = p.thisObject.getClass().getMethod("getView").invoke(p.thisObject);
                Events.emit("snackbar", "text", Dump.joinTexts((View) view, 4));
            })));

        install("log_errors", () -> {
            Class<?> log = android.util.Log.class;
            XC_MethodHook error = after(p -> {
                if (p.args.length < 2) return;
                Throwable tr = p.args[p.args.length - 1] instanceof Throwable ? (Throwable) p.args[p.args.length - 1] : null;
                Events.logError(String.valueOf(p.args[0]), p.args[1] instanceof String ? (String) p.args[1] : null, tr);
            });
            all(log, "e", error);
            all(log, "wtf", error);
            all(log, "println", after(p -> {
                if (p.args.length == 3 && p.args[0] instanceof Integer && (Integer) p.args[0] >= android.util.Log.ERROR) {
                    Events.logError(String.valueOf(p.args[1]), String.valueOf(p.args[2]), null);
                }
            }));
        });

        install("crash", () -> all(Thread.class, "dispatchUncaughtException", before(p -> {
            Throwable t = (Throwable) p.args[0];
            Events.emit("crash", "thread", ((Thread) p.thisObject).getName(),
                "exception", t.getClass().getName(), "message", String.valueOf(t.getMessage()),
                "at", t.getStackTrace().length > 0 ? t.getStackTrace()[0].toString() : "");
        })));

        install("intents", () -> all(android.app.Instrumentation.class, "execStartActivity", before(p -> {
            for (Object a : p.args) {
                if (a instanceof Intent) { Events.intent((Intent) a); return; }
            }
        })));

        install("okhttp", () -> {
            Class<?> client = Class.forName("okhttp3.OkHttpClient", false, cl);
            Class<?> interceptor = Class.forName("okhttp3.Interceptor", false, cl);
            // The recorder calls Chain.request() and Chain.proceed(Request) by name; an app whose
            // R8 renamed them gets no interceptor (the hook reports why) rather than failed requests.
            Class<?> chain = Class.forName("okhttp3.Interceptor$Chain", false, cl);
            chain.getMethod("request");
            if (Recorder.proceed(chain) == null) throw new NoSuchMethodError("okhttp3.Interceptor$Chain.proceed");
            if (XposedBridge.hookAllConstructors(client, before(p -> {
                for (Object a : p.args) {
                    if (a != null && a.getClass().getName().endsWith("OkHttpClient$Builder")) addInterceptor(a, interceptor);
                }
            })).isEmpty()) throw new NoSuchMethodError("OkHttpClient()");
        });
        // An app whose build shortened OkHttp's class names: found by shape instead, a few seconds after start.
        if (!"ok".equals(hooks.get("okhttp")) && OkHttpByShape.wanted(pkg)) {
            install(OkHttpByShape.HOOK, () -> {
                OkHttpByShape.install(cl, lp.appInfo);
                throw new IllegalStateException("looking for it");  // the worker writes the outcome
            });
        }

        // Pages inside WebViews: remember each WebView (snapshots read their pages) and hook the
        // app's own WebViewClient / WebChromeClient classes for load, error and alert() events.
        install("webview", () -> {
            if (XposedBridge.hookAllConstructors(android.webkit.WebView.class,
                after(p -> Web.register((android.webkit.WebView) p.thisObject))).isEmpty()) {
                throw new NoSuchMethodError("WebView()");
            }
        });
        install("webview_clients", () -> {
            all(android.webkit.WebView.class, "setWebViewClient", after(p -> Web.hookClient(p.args[0], false)));
            all(android.webkit.WebView.class, "setWebChromeClient", after(p -> Web.hookClient(p.args[0], true)));
        });

        Server.start();
    }

    static void onResumed(Activity a) {
        if (resumed.get() != a) Events.emit("activity_resumed", "activity", a.getClass().getName());
        resumed = new WeakReference<>(a);
    }

    /** One application interceptor per OkHttp client: keeps a short
     * copy of each response for the `network` command. Certificate pinning is irrelevant here:
     * the interceptor runs inside the app, after TLS. */
    static void addInterceptor(Object builder, Class<?> interceptor) throws Exception {
        @SuppressWarnings("unchecked")
        List<Object> list = (List<Object>) builder.getClass().getMethod("interceptors").invoke(builder);
        for (Object o : list) {
            if (o.getClass().getName().equals(INTERCEPTOR_CLASS)) return;
        }
        list.add(interceptorClass(interceptor.getClassLoader()).getConstructor(InvocationHandler.class).newInstance(new Recorder()));
    }

    static final String INTERCEPTOR_CLASS = "app.relay.bridge.okhttp.RecorderInterceptor";
    private static final Map<ClassLoader, Class<?>> interceptorClasses = Collections.synchronizedMap(new WeakHashMap<>());

    /** The interceptor class (shim/RecorderInterceptor.java, dexed by build.py) defined in the
     * class loader that holds the app's okhttp3, so it implements the app's Interceptor. Not a
     * java.lang.reflect.Proxy: a proxy may only throw the checked exceptions the interface
     * method declares, and R8 strips those declarations, so a request's IOException came back
     * as UndeclaredThrowableException and killed the app (Kleinanzeigen, every ad request). */
    static Class<?> interceptorClass(ClassLoader app) throws Exception {
        Class<?> c = interceptorClasses.get(app);
        if (c == null) {
            byte[] dex = android.util.Base64.decode(InterceptorDex.BASE64, android.util.Base64.DEFAULT);
            c = new dalvik.system.InMemoryDexClassLoader(java.nio.ByteBuffer.wrap(dex), app).loadClass(INTERCEPTOR_CLASS);
            interceptorClasses.put(app, c);
        }
        return c;
    }

    /** What the interceptor does, called from the app's class loader through InvocationHandler
     * (the one interface both loaders share) with a null method and the Chain as args[0]. Any
     * Throwable thrown here reaches OkHttp as is; an IOException is a plain network failure. */
    static final class Recorder implements InvocationHandler {
        @Override public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
            if (method != null && !"intercept".equals(method.getName())) {
                switch (method.getName()) {
                    case "hashCode": return System.identityHashCode(proxy);
                    case "equals": return proxy == args[0];
                    case "toString": return "RelayBridgeInterceptor";
                    default: return null;
                }
            }
            Object chain = args[0];
            Object request;
            Method proceed;
            try {
                request = call(chain, "request");
                proceed = proceed(chain.getClass());
                if (proceed == null) throw new NoSuchMethodException(chain.getClass().getName() + ".proceed");
            } catch (Throwable t) {  // the chain class is not what the install-time check saw
                Problems.record("okhttp_chain", chain.getClass().getName(), t);
                throw new java.io.IOException("relay bridge: cannot reach OkHttp's chain in " + chain.getClass().getName(), t);
            }
            long started = SystemClock.uptimeMillis();
            Object response;
            try {
                response = proceed.invoke(chain, request);
            } catch (java.lang.reflect.InvocationTargetException e) {
                Events.http(request, null, SystemClock.uptimeMillis() - started, e.getCause());
                throw e.getCause();
            }
            Events.http(request, response, SystemClock.uptimeMillis() - started, null);
            return response;
        }

        /** Chain.proceed(Request), on the interface or an implementation; null when renamed. */
        static Method proceed(Class<?> chain) {
            for (Method m : chain.getMethods()) {
                if (m.getName().equals("proceed") && m.getParameterTypes().length == 1) {
                    m.setAccessible(true);
                    return m;
                }
            }
            return null;
        }
    }

    static Object call(Object target, String method) throws Exception {
        Method m = target.getClass().getMethod(method);
        m.setAccessible(true);
        return m.invoke(target);
    }
}
