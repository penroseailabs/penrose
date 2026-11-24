package app.relay.bridge;

import android.content.pm.ApplicationInfo;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedBridge;

/**
 * OkHttp in an app whose build shortened its class names (okhttp3.a, okhttp3.b0...), where the hook by name
 * (Module, "okhttp") finds nothing: 6 of the 19 apps tried.
 *
 * The names are gone but the shape is not. Response is the one class in the okhttp3 package that is Closeable and
 * holds several fields of its own type (the network, cache and prior responses) and a single int (the status).
 * Every response an app gets is built through its constructor, so one hook there sees each exchange, whenever the
 * app made its client. The method and the address are read from the request by what the fields hold (a String
 * that is an HTTP method, a value that prints as a URL), never by name.
 *
 * It tells less than the hook by name: method, address and status only, as "http" events. No body, no timing, no
 * transport failure, so nothing reaches the `network` command from these apps.
 *
 * Finding the class means reading the names of the app's classes from its APK, which costs about a second of
 * processor time. It is done once per app version, off the main thread and after the app has started; the answer
 * is kept in a small file in the app's own cache folder.
 */
final class OkHttpByShape {
    static final String HOOK = "okhttp_renamed";
    private static final byte[] PREFIX = "Lokhttp3/".getBytes(StandardCharsets.US_ASCII);
    private static final int START_AFTER_MS = 4000;
    private static final int SAME_WITHIN_MS = 3000;  // one exchange builds its response several times over
    private static final int GIVE_UP_AFTER = 40;

    private static Field codeField, requestField, methodField, urlField;
    private static final AtomicInteger unread = new AtomicInteger();
    private static final Map<String, Long> recent = new LinkedHashMap<String, Long>() {
        @Override protected boolean removeEldestEntry(Map.Entry<String, Long> eldest) { return size() > 64; }
    };

    /** Whether to look at all: Android's and Google's own apps do not carry OkHttp this way. */
    static boolean wanted(String pkg) {
        return !(pkg.equals("android") || pkg.startsWith("com.android.") || pkg.startsWith("com.google."));
    }

    static void install(final ClassLoader loader, final ApplicationInfo app) {
        Module.fired.putIfAbsent(HOOK, new AtomicInteger());
        Thread worker = new Thread(() -> {
            try {
                Thread.sleep(START_AFTER_MS);
                String name = known(app);
                if (name == null) {
                    name = find(loader, app);
                    remember(app, name);
                }
                if (name.isEmpty()) {
                    Module.hooks.put(HOOK, "no Response class in okhttp3");
                    return;
                }
                Class<?> response = Class.forName(name, false, loader);
                if (!looksLikeResponse(response)) throw new ClassNotFoundException(name + " is not OkHttp's Response");
                if (XposedBridge.hookAllConstructors(response, new XC_MethodHook() {
                    @Override protected void afterHookedMethod(MethodHookParam p) {
                        Module.fired.get(HOOK).incrementAndGet();
                        seen(p.thisObject);
                    }
                }).isEmpty()) throw new NoSuchMethodError(name + "()");
                Module.hooks.put(HOOK, "ok");
            } catch (Throwable t) {
                Module.hooks.put(HOOK, t.getClass().getSimpleName() + ": " + t.getMessage());
            }
        }, "relay-okhttp-shape");
        worker.setPriority(Thread.MIN_PRIORITY);
        worker.setDaemon(true);
        worker.start();
    }

    // --- finding Response -------------------------------------------------------------------------------

    /** The class name ("" when the app has none), searching the base APK and then its splits. */
    private static String find(ClassLoader loader, ApplicationInfo app) throws Exception {
        List<String> apks = new ArrayList<>();
        apks.add(app.sourceDir);
        if (app.splitSourceDirs != null) for (String split : app.splitSourceDirs) apks.add(split);
        for (String apk : apks) {
            for (String name : classesInOkHttp(apk)) {
                try {
                    if (looksLikeResponse(Class.forName(name, false, loader))) return name;
                } catch (Throwable ignored) {  // a class that does not load is not the one
                }
            }
        }
        return "";
    }

    static boolean looksLikeResponse(Class<?> c) {
        if (c.isInterface() || !java.io.Closeable.class.isAssignableFrom(c)) return false;
        int own = 0, ints = 0;
        for (Field f : c.getDeclaredFields()) {
            if (Modifier.isStatic(f.getModifiers())) continue;
            if (f.getType() == c) own++;
            else if (f.getType() == int.class) ints++;
        }
        return own >= 2 && ints == 1;
    }

    /** The classes directly in the okhttp3 package, read from the class tables of the APK's dex files. */
    private static List<String> classesInOkHttp(String apk) throws Exception {
        List<String> names = new ArrayList<>();
        try (ZipFile zip = new ZipFile(apk)) {
            for (int i = 1; ; i++) {
                ZipEntry entry = zip.getEntry(i == 1 ? "classes.dex" : "classes" + i + ".dex");
                if (entry == null) break;
                try (InputStream in = zip.getInputStream(entry)) {
                    collect(readAll(in, (int) Math.max(entry.getSize(), 1 << 16)), names);
                }
            }
        }
        return names;
    }

    private static byte[] readAll(InputStream in, int expected) throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream(expected);
        byte[] chunk = new byte[1 << 16];
        for (int n; (n = in.read(chunk)) > 0; ) out.write(chunk, 0, n);
        return out.toByteArray();
    }

    /** class_defs -> type_ids -> string_ids -> the descriptor ("Lokhttp3/b0;"), as the dex format lays them out. */
    private static void collect(byte[] dex, List<String> names) {
        if (dex.length < 0x70 || dex[0] != 'd' || dex[1] != 'e' || dex[2] != 'x') return;
        ByteBuffer b = ByteBuffer.wrap(dex).order(ByteOrder.LITTLE_ENDIAN);
        int strings = b.getInt(0x3C), types = b.getInt(0x44), count = b.getInt(0x60), classes = b.getInt(0x64);
        for (int i = 0; i < count; i++) {
            int at = b.getInt(strings + 4 * b.getInt(types + 4 * b.getInt(classes + 32 * i)));
            while ((dex[at++] & 0x80) != 0) { }  // the length, which the terminating zero makes unnecessary
            boolean ours = true;
            for (int k = 0; k < PREFIX.length && ours; k++) ours = dex[at + k] == PREFIX[k];
            if (!ours) continue;
            int end = at;
            while (dex[end] != 0) end++;
            String name = new String(dex, at + 1, end - at - 2, StandardCharsets.UTF_8);  // okhttp3/b0
            if (name.indexOf('/', PREFIX.length - 1) < 0) names.add(name.replace('/', '.'));  // not a subpackage
        }
    }

    // --- what was found, kept for the app's next start --------------------------------------------------

    private static File note(ApplicationInfo app) {
        return new File(new File(app.dataDir, "cache"), "relay-bridge-okhttp");
    }

    /** The name found for this version of the app ("" for none), or null when it has not been looked for. */
    private static String known(ApplicationInfo app) {
        try (FileInputStream in = new FileInputStream(note(app))) {
            String[] lines = new String(readAll(in, 256), StandardCharsets.UTF_8).split("\n", -1);
            if (lines.length >= 2 && lines[0].equals(version(app))) return lines[1].trim();
        } catch (Throwable ignored) {
        }
        return null;
    }

    private static void remember(ApplicationInfo app, String name) {
        try {
            File file = note(app);
            file.getParentFile().mkdirs();
            try (FileOutputStream out = new FileOutputStream(file)) {
                out.write((version(app) + "\n" + name + "\n").getBytes(StandardCharsets.UTF_8));
            }
        } catch (Throwable ignored) {  // then it is looked for again next time
        }
    }

    private static String version(ApplicationInfo app) {
        // The install path is new with every update of the app, so path and date stand for its version.
        return Module.VERSION + " " + app.sourceDir + " " + new File(app.sourceDir).lastModified();
    }

    // --- reading a response -----------------------------------------------------------------------------

    private static void seen(Object response) {
        try {
            if (methodField == null) {
                if (unread.get() > GIVE_UP_AFTER || !learn(response)) {
                    unread.incrementAndGet();
                    return;
                }
            }
            Object request = requestField.get(response);
            if (request == null) return;
            String method = String.valueOf(methodField.get(request));
            String url = String.valueOf(urlField.get(request));
            int code = codeField.getInt(response);
            String key = method + " " + url + " " + code;
            long now = android.os.SystemClock.uptimeMillis();
            synchronized (recent) {
                Long last = recent.get(key);
                recent.put(key, now);
                if (last != null && now - last < SAME_WITHIN_MS) return;
            }
            Events.emit("http", "method", method, "url", Events.clip(url, 300), "code", code);
        } catch (Throwable t) {
            if (unread.incrementAndGet() == 1) Problems.record("okhttp_renamed", response.getClass().getName(), t);
        }
    }

    /** Which fields hold the status, the request, its method and its address, from one live response. */
    private static synchronized boolean learn(Object response) throws Exception {
        if (methodField != null) return true;
        Class<?> c = response.getClass();
        Field code = null;
        for (Field f : c.getDeclaredFields()) {
            if (!Modifier.isStatic(f.getModifiers()) && f.getType() == int.class) code = f;
        }
        if (code == null) return false;
        for (Field f : c.getDeclaredFields()) {
            Class<?> type = f.getType();
            if (Modifier.isStatic(f.getModifiers()) || type == c || type.isEnum() || type.isInterface()
                || !type.getName().startsWith("okhttp3.")) continue;
            f.setAccessible(true);
            Object candidate = f.get(response);
            if (candidate == null) continue;
            Field method = null, url = null;
            for (Field g : candidate.getClass().getDeclaredFields()) {
                if (Modifier.isStatic(g.getModifiers())) continue;
                g.setAccessible(true);
                Object value = g.get(candidate);
                if (value == null) continue;
                if (value instanceof String) {
                    if (method == null && ((String) value).matches("[A-Z]{3,7}")) method = g;
                } else if (url == null && g.getType().getName().startsWith("okhttp3.") && value.toString().startsWith("http")) {
                    url = g;
                }
            }
            if (method != null && url != null) {
                code.setAccessible(true);
                codeField = code;
                requestField = f;
                urlField = url;
                methodField = method;  // last: seen() reads the others once this is set
                return true;
            }
        }
        return false;
    }
}
