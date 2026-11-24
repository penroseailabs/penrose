package app.relay.bridge;

import java.util.HashMap;
import java.util.Map;
import java.util.Set;

import de.robv.android.xposed.XC_MethodHook;

/**
 * Shows a few apps a real phone (a Pixel 9 on Android 15, the image's own version) instead of the emulator.
 *
 * Google Meet (in Gmail and in its own app) switches its call audio to "dummy" devices when it finds an emulator
 * ("Running emulator, overriding sample rate to 8 kHz" ... "Dummy Audio APIs will be utilized"), so calls placed
 * from penrose's phone carry no sound either way. Only the apps listed here see the profile: everything else, and
 * Android itself, still sees the emulator.
 */
final class DeviceProfile {
    static final Set<String> APPS = Set.of(
            "com.google.android.gm",                // Meet inside Gmail
            "com.google.android.apps.tachyon",      // Google Meet
            "com.google.android.apps.meetings");    // Meet (older package)

    static final Map<String, String> BUILD = new HashMap<>();
    static final Map<String, String> PROPS = new HashMap<>();

    static {
        String fingerprint = "google/tokay/tokay:15/AP4A.250205.002/12821496:user/release-keys";
        BUILD.put("BRAND", "google");
        BUILD.put("MANUFACTURER", "Google");
        BUILD.put("MODEL", "Pixel 9");
        BUILD.put("DEVICE", "tokay");
        BUILD.put("PRODUCT", "tokay");
        BUILD.put("BOARD", "tokay");
        BUILD.put("HARDWARE", "tokay");
        BUILD.put("BOOTLOADER", "ripcurrent-15.0-12620012");
        BUILD.put("FINGERPRINT", fingerprint);
        BUILD.put("TAGS", "release-keys");
        BUILD.put("TYPE", "user");
        BUILD.put("HOST", "abfarm-release-2004-0052");

        PROPS.put("ro.kernel.qemu", "");
        PROPS.put("ro.boot.qemu", "");
        PROPS.put("ro.kernel.qemu.gles", "");
        PROPS.put("ro.boot.qemu.gles", "");
        PROPS.put("ro.hardware", "tokay");
        PROPS.put("ro.boot.hardware", "tokay");
        PROPS.put("ro.product.board", "tokay");
        PROPS.put("ro.build.characteristics", "nosdcard");
        PROPS.put("ro.build.fingerprint", fingerprint);
        PROPS.put("ro.build.tags", "release-keys");
        PROPS.put("ro.build.type", "user");
        for (String part : new String[]{"", "system.", "vendor.", "product.", "odm.", "system_ext.", "bootimage."}) {
            PROPS.put("ro.product." + part + "brand", "google");
            PROPS.put("ro.product." + part + "manufacturer", "Google");
            PROPS.put("ro.product." + part + "model", "Pixel 9");
            PROPS.put("ro.product." + part + "device", "tokay");
            PROPS.put("ro.product." + part + "name", "tokay");
            PROPS.put("ro." + part + "build.fingerprint", fingerprint);
        }
    }

    private DeviceProfile() {
    }

    static boolean wanted(String pkg) {
        return APPS.contains(pkg);
    }

    static void install() throws Throwable {
        for (Map.Entry<String, String> e : BUILD.entrySet()) {
            java.lang.reflect.Field field = android.os.Build.class.getField(e.getKey());
            field.setAccessible(true);
            field.set(null, e.getValue());
        }
        Class<?> props = Class.forName("android.os.SystemProperties");
        XC_MethodHook swap = new XC_MethodHook() {
            @Override
            protected void beforeHookedMethod(MethodHookParam p) {
                if (p.args.length == 0 || !(p.args[0] instanceof String)) return;
                String value = PROPS.get((String) p.args[0]);
                if (value == null) return;
                Class<?> type = ((java.lang.reflect.Method) p.method).getReturnType();
                if (type == String.class) {
                    p.setResult(value.isEmpty() && p.args.length > 1 ? p.args[1] : value);
                } else if (type == boolean.class) {
                    p.setResult(value.isEmpty() ? (p.args.length > 1 ? p.args[1] : false) : Boolean.parseBoolean(value));
                } else if (type == int.class || type == long.class) {
                    p.setResult(p.args.length > 1 ? p.args[1] : (type == int.class ? (Object) 0 : (Object) 0L));
                }
            }
        };
        Module.all(props, "get", swap);
        Module.all(props, "getBoolean", swap);
        Module.all(props, "getInt", swap);
        Module.all(props, "getLong", swap);
    }
}
