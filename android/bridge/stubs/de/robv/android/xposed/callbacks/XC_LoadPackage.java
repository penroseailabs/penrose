package de.robv.android.xposed.callbacks;

import android.content.pm.ApplicationInfo;

/** Compile-only stub; the real class is provided by the Xposed framework at runtime. */
public abstract class XC_LoadPackage {
    public static final class LoadPackageParam {
        public String packageName;
        public String processName;
        public ClassLoader classLoader;
        public ApplicationInfo appInfo;
        public boolean isFirstApplication;
    }
}
