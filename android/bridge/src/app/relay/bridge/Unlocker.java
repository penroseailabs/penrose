package app.relay.bridge;

import android.app.KeyguardManager;
import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.os.UserManager;
import android.util.Log;
import android.view.View;
import android.view.ViewGroup;
import android.view.inputmethod.EditorInfo;
import android.widget.EditText;

import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.Collections;
import java.util.Map;
import java.util.WeakHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedBridge;

/**
 * Loaded in SystemUI only. The device may keep a real screen lock (a PIN, so passkeys and other
 * credential-bound features work); this makes it invisible, so neither the phone agent nor the
 * supervising agent ever sees a lock screen or knows the PIN. Everything is driven by SystemUI's
 * own events, nothing polls the screen and no key is pressed:
 *
 * - Lock screen coming up while the user's storage is already unlocked (screen off, timeout,
 *   power key): it is not shown at all (KeyguardViewMediator.doKeyguardLocked is skipped).
 * - Lock screen shown anyway (at boot, before the PIN has unlocked the user's credential-encrypted
 *   storage, or when SystemUI restarts): once SystemUI is ready, the PIN is checked with
 *   LockPatternUtils.checkCredential on a background thread, exactly what the bouncer does with a
 *   typed PIN (it unlocks the storage and the keystore), and the lock screen is then finished the
 *   way a correct PIN finishes it (KeyguardViewMediator.tryKeyguardDone). No bouncer is drawn.
 *   Finishing it before SystemUI is ready left SystemUI's shade window over the screen taking every
 *   touch (seen 2026-10-25), so both steps wait for mSystemReady. The screen going off cancels the
 *   finish, so a lock screen still up when the screen comes on again is finished then.
 * - Credential prompts (BiometricPrompt with device credential, as passkey user verification
 *   uses): the PIN field is filled and submitted as soon as it is ready. The prompt must produce
 *   its own gatekeeper token for its caller, so it goes through its own check.
 *
 * The PIN is `persist.relay.lock_pin`, 0000 when unset. A wrong PIN counts toward the system's
 * lockout, so after two refused checks in a row (per kind) it stops and logs, tag relay_unlocker.
 */
final class Unlocker {
    static final String TAG = "relay_unlocker";
    private static final int MAX_FAILED_TRIES = 2;
    /** Time the lock screen gets to go away after tryKeyguardDone before the fallback dismiss. */
    private static final long DONE_VERDICT_MS = 1500;
    /** A filled prompt still on screen this long after submitting was refused. */
    private static final long PROMPT_VERDICT_MS = 3000;
    private static final long PROMPT_POLL_MS = 50;
    private static final int PROMPT_POLLS = 100;

    private static volatile Object mediator;
    private static volatile Object lockPatternUtils;
    /** True once this SystemUI has checked the PIN successfully: the bouncer may then be skipped. */
    private static volatile boolean verified;
    private static volatile int keyguardFailures;
    private static volatile String failedPin = "";
    private static volatile int promptFailures;
    private static final AtomicBoolean unlocking = new AtomicBoolean();
    private static final AtomicBoolean wakeHooked = new AtomicBoolean();
    private static final ExecutorService worker = Executors.newSingleThreadExecutor(r -> {
        Thread t = new Thread(r, "relay-unlocker");
        t.setDaemon(true);
        return t;
    });
    private static final Handler main = new Handler(Looper.getMainLooper());
    private static final Map<View, Boolean> filledPrompts = Collections.synchronizedMap(new WeakHashMap<>());
    private static long skipsLogged;

    private static boolean installed;

    static synchronized void install(ClassLoader cl) {
        // SystemUI's process loads a second package under the same name later (its class loader
        // lacks the keyguard classes); the first load is the one to hook.
        if (installed) return;
        installed = true;
        Module.install("unlock_keyguard", () -> {
            Class<?> kvm = Class.forName("com.android.systemui.keyguard.KeyguardViewMediator", false, cl);
            if (XposedBridge.hookAllConstructors(kvm, Module.after(p -> {
                mediator = p.thisObject;
                lockPatternUtils = field(p.thisObject, "mLockPatternUtils").get(p.thisObject);
            })).isEmpty()) throw new NoSuchMethodError(kvm.getName() + ".<init>");
            Module.all(kvm, "doKeyguardLocked", new XC_MethodHook() {
                @Override protected void beforeHookedMethod(MethodHookParam p) {
                    try { beforeDoKeyguard(p); } catch (Throwable t) { Log.e(TAG, "doKeyguardLocked", t); }
                }
                @Override protected void afterHookedMethod(MethodHookParam p) {
                    if (!skipped.get() && systemReady() && showing()) unlockSoon("shown");
                    skipped.set(false);
                }
            });
            Module.all(kvm, "setShowingLocked", Module.after(p -> {
                if (Boolean.TRUE.equals(p.args[0]) && systemReady()) unlockSoon("shown");
            }));
        });
        // A lock screen still up when the screen comes on. Finishing it is cancelled when the screen goes off
        // first ("Device is going to sleep, aborting keyguardDone"): the lock screen turns the screen off after
        // 10 s on its own, and a PIN check on a busy phone took longer (2026-10-28, right after a boot). Nothing
        // else would show it again, so it stayed up with the PIN already accepted.
        // Hooked on IKeyguardService, the interface the system calls SystemUI through (its method names do not
        // change with SystemUI's build, KeyguardViewMediator's do); SystemUI implements it in an anonymous
        // class, found from the binder KeyguardService hands out.
        Module.install("unlock_on_wake", () -> {
            Class.forName("com.android.internal.policy.IKeyguardService", false, cl).getMethod("onFinishedWakingUp");
            XC_MethodHook woke = Module.after(p -> {
                if (systemReady() && showing()) unlockSoon("woke");
            });
            Module.all(Class.forName("com.android.systemui.keyguard.KeyguardService", false, cl), "onBind",
                Module.after(p -> {
                    Object binder = p.getResult();
                    if (binder != null && wakeHooked.compareAndSet(false, true)) {
                        Module.all(binder.getClass(), "onFinishedWakingUp", woke);
                    }
                }));
        });
        Module.install("unlock_skip_bouncer", () -> Module.all(
            Class.forName("com.android.keyguard.KeyguardUpdateMonitor", false, cl), "getUserCanSkipBouncer",
            Module.after(p -> {
                if (verified && userUnlocked()) p.setResult(true);
            })));
        Module.install("unlock_prompt", () -> Module.all(
            Class.forName("com.android.systemui.biometrics.ui.CredentialPasswordView", false, cl), "init",
            Module.after(p -> {
                View view = (View) p.thisObject;
                view.post(() -> fillPrompt(view, 0));
            })));
        Log.i(TAG, "installed: " + Module.hooks);
    }

    static String pin() {
        try {
            String pin = (String) Class.forName("android.os.SystemProperties")
                .getMethod("get", String.class, String.class).invoke(null, "persist.relay.lock_pin", "");
            if (pin != null && !pin.trim().isEmpty()) return pin.trim();
        } catch (Throwable ignored) {
        }
        return "0000";
    }

    /**
     * The lock screen is about to come up; with the storage already unlocked there is no need. Only once
     * SystemUI is ready and while no lock screen is showing (screen off, timeout, power key): the call
     * SystemUI makes when it becomes ready sets its lock screen up and must run (unlockSoon follows it).
     */
    private static void beforeDoKeyguard(XC_MethodHook.MethodHookParam p) {
        if (!systemReady() || showing() || !secure() || !userUnlocked()) return;
        p.setResult(null);
        skipped.set(true);
        if (skipsLogged++ < 3) Log.i(TAG, "keyguard: not shown, the device is already unlocked");
    }

    /** Set by beforeDoKeyguard when it skipped the call, for the after hook on the same thread. */
    private static final ThreadLocal<Boolean> skipped = ThreadLocal.withInitial(() -> false);

    private static boolean systemReady() {
        return flag("mSystemReady");
    }

    private static boolean showing() {
        return flag("mShowing");
    }

    private static boolean flag(String name) {
        Object m = mediator;
        try {
            return m != null && field(m, name).getBoolean(m);
        } catch (Throwable t) {
            return false;
        }
    }

    /** Checks the PIN off the main thread, then finishes the lock screen as a correct PIN would. */
    private static void unlockSoon(String why) {
        if (!unlocking.compareAndSet(false, true)) return;
        final long start = SystemClock.uptimeMillis();
        worker.execute(() -> {
            try {
                if (!secure()) return;
                String pin = pin();
                if (!pin.equals(failedPin)) keyguardFailures = 0;
                if (keyguardFailures >= MAX_FAILED_TRIES) {
                    Log.e(TAG, "keyguard: not tried, the PIN was refused " + keyguardFailures + " times; is persist.relay.lock_pin the device's PIN?");
                    return;
                }
                if (!checkPin(pin)) {
                    failedPin = pin;
                    keyguardFailures++;
                    Log.e(TAG, "keyguard: the PIN was refused (" + keyguardFailures + " in a row)");
                    return;
                }
                keyguardFailures = 0;
                verified = true;
                long checked = SystemClock.uptimeMillis();
                main.post(() -> finish(start, checked, why));
            } catch (Throwable t) {
                Log.e(TAG, "keyguard: could not unlock", t);
            } finally {
                unlocking.set(false);
            }
        });
    }

    /** Finishes the lock screen the way a correct PIN on the bouncer does (KeyguardViewMediator.tryKeyguardDone). */
    private static void finish(long start, long checked, String why) {
        try {
            method(mediator, "tryKeyguardDone").invoke(mediator);
        } catch (Throwable t) {
            Log.w(TAG, "keyguard: tryKeyguardDone failed, dismissing instead", t);
            dismiss();
        }
        verdict(start, checked, why, 0);
    }

    /** Logs when the lock screen is gone; still up after DONE_VERDICT_MS, it is dismissed through WindowManager. */
    private static void verdict(long start, long checked, String why, int checks) {
        if (!keyguardLocked()) {
            Log.i(TAG, "keyguard: unlocked (" + why + "), PIN checked in " + (checked - start)
                + " ms, gone after " + (SystemClock.uptimeMillis() - start) + " ms");
        } else if (checks * 25L >= DONE_VERDICT_MS) {
            Log.w(TAG, "keyguard: still up " + DONE_VERDICT_MS + " ms after tryKeyguardDone, dismissing");
            dismiss();
        } else {
            main.postDelayed(() -> verdict(start, checked, why, checks + 1), 25);
        }
    }

    /** The device's own dismiss; with the PIN checked, getUserCanSkipBouncer lets it through without a bouncer. */
    private static void dismiss() {
        try {
            Object wm = Class.forName("android.view.WindowManagerGlobal").getMethod("getWindowManagerService").invoke(null);
            for (Method m : wm.getClass().getMethods()) {
                if (m.getName().equals("dismissKeyguard")) {
                    m.invoke(wm, new Object[m.getParameterTypes().length]);
                    return;
                }
            }
        } catch (Throwable t) {
            Log.e(TAG, "keyguard: dismiss failed", t);
        }
    }

    /** LockPatternUtils.checkCredential: the bouncer's own check of a typed PIN. */
    private static boolean checkPin(String pin) throws Throwable {
        Object lpu = lockPatternUtils;
        if (lpu == null) throw new IllegalStateException("no LockPatternUtils yet");
        Class<?> credClass = Class.forName("com.android.internal.widget.LockscreenCredential");
        Object credential = credClass.getMethod("createPin", CharSequence.class).invoke(null, pin);
        try {
            for (Method m : lpu.getClass().getMethods()) {
                if (m.getName().equals("checkCredential") && m.getParameterTypes().length == 3) {
                    return (Boolean) m.invoke(lpu, credential, userId(), null);
                }
            }
            throw new NoSuchMethodError("LockPatternUtils.checkCredential");
        } finally {
            try { credClass.getMethod("zeroize").invoke(credential); } catch (Throwable ignored) { }
        }
    }

    private static Context context() {
        try {
            return (Context) Class.forName("android.app.ActivityThread").getMethod("currentApplication").invoke(null);
        } catch (Throwable t) {
            return null;
        }
    }

    private static int userId() {
        try {
            return (Integer) Class.forName("android.app.ActivityManager").getMethod("getCurrentUser").invoke(null);
        } catch (Throwable t) {
            return 0;
        }
    }

    private static boolean secure() {
        try {
            return (Boolean) lockPatternUtils.getClass().getMethod("isSecure", int.class).invoke(lockPatternUtils, userId());
        } catch (Throwable t) {
            Context ctx = context();
            return ctx != null && ctx.getSystemService(KeyguardManager.class).isDeviceSecure();
        }
    }

    private static boolean userUnlocked() {
        Context ctx = context();
        return ctx != null && ctx.getSystemService(UserManager.class).isUserUnlocked();
    }

    private static boolean keyguardLocked() {
        Context ctx = context();
        return ctx != null && ctx.getSystemService(KeyguardManager.class).isKeyguardLocked();
    }

    /** Fills a credential prompt's PIN field once and submits it; a prompt still open afterwards was refused. */
    private static void fillPrompt(View view, int polls) {
        if (filledPrompts.containsKey(view)) return;
        EditText field = findEditText(view);
        // The binder focuses the field in the same step that attaches its submit listener.
        if (field == null || !field.isFocused()) {
            if (polls < PROMPT_POLLS) {
                view.postDelayed(() -> fillPrompt(view, polls + 1), PROMPT_POLL_MS);
            } else if (view.isAttachedToWindow()) {
                Log.w(TAG, "prompt: its PIN field never became ready: " + view);
            }
            return;
        }
        filledPrompts.put(view, true);
        if (promptFailures >= MAX_FAILED_TRIES) {
            Log.e(TAG, "prompt: not filled, the last " + promptFailures + " were refused; is persist.relay.lock_pin the device's PIN?");
            return;
        }
        Log.i(TAG, "prompt: entering the PIN");
        field.setText(pin());
        field.onEditorAction(EditorInfo.IME_ACTION_DONE);
        view.postDelayed(() -> {
            if (view.isAttachedToWindow()) {
                promptFailures++;
                Log.w(TAG, "prompt: still open after the PIN; refused " + promptFailures + " in a row");
            } else {
                promptFailures = 0;
            }
        }, PROMPT_VERDICT_MS);
    }

    private static EditText findEditText(View v) {
        if (v instanceof EditText) return (EditText) v;
        if (v instanceof ViewGroup) {
            ViewGroup g = (ViewGroup) v;
            for (int i = 0; i < g.getChildCount(); i++) {
                EditText found = findEditText(g.getChildAt(i));
                if (found != null) return found;
            }
        }
        return null;
    }

    private static Method method(Object target, String name, Class<?>... params) throws NoSuchMethodException {
        for (Class<?> c = target.getClass(); c != null; c = c.getSuperclass()) {
            try {
                Method m = c.getDeclaredMethod(name, params);
                m.setAccessible(true);
                return m;
            } catch (NoSuchMethodException ignored) {
            }
        }
        throw new NoSuchMethodException(target.getClass().getName() + "." + name);
    }

    private static Field field(Object target, String name) throws NoSuchFieldException {
        for (Class<?> c = target.getClass(); c != null; c = c.getSuperclass()) {
            try {
                Field f = c.getDeclaredField(name);
                f.setAccessible(true);
                return f;
            } catch (NoSuchFieldException ignored) {
            }
        }
        throw new NoSuchFieldException(target.getClass().getName() + "." + name);
    }
}
