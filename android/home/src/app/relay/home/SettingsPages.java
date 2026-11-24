package app.relay.home;

import android.provider.Settings;

import java.util.List;

/** The Settings screens by name, so "bluetooth" lands on the Bluetooth page instead of the Settings home. */
final class SettingsPages implements Source {
    static final String[][] PAGES = {
        {"Wi-Fi", Settings.ACTION_WIFI_SETTINGS}, {"Bluetooth", Settings.ACTION_BLUETOOTH_SETTINGS},
        {"Location", Settings.ACTION_LOCATION_SOURCE_SETTINGS}, {"Display", Settings.ACTION_DISPLAY_SETTINGS},
        {"Sound", Settings.ACTION_SOUND_SETTINGS}, {"Apps", Settings.ACTION_APPLICATION_SETTINGS},
        {"Battery", Settings.ACTION_BATTERY_SAVER_SETTINGS}, {"Date and time", Settings.ACTION_DATE_SETTINGS},
        {"Language and input", Settings.ACTION_LOCALE_SETTINGS}, {"Accounts", Settings.ACTION_SYNC_SETTINGS},
        {"Developer options", Settings.ACTION_APPLICATION_DEVELOPMENT_SETTINGS}, {"Storage", Settings.ACTION_INTERNAL_STORAGE_SETTINGS},
        {"Security", Settings.ACTION_SECURITY_SETTINGS}, {"Accessibility", Settings.ACTION_ACCESSIBILITY_SETTINGS},
        {"Notifications", "android.settings.NOTIFICATION_SETTINGS"}, {"Default apps", Settings.ACTION_MANAGE_DEFAULT_APPS_SETTINGS},
        {"Airplane mode", Settings.ACTION_AIRPLANE_MODE_SETTINGS}, {"Mobile network", Settings.ACTION_NETWORK_OPERATOR_SETTINGS},
        {"Settings", Settings.ACTION_SETTINGS},
    };

    @Override
    public void search(Query q, List<Result> out) {
        if (q.isEmpty()) return;
        for (String[] p : PAGES) {
            int s = q.score(p[0] + " settings");
            if (s > 0 && (q.score(p[0]) > 0 || q.tokens.size() > 1))
                out.add(new Result(Result.SETTING, p[0], "Settings", s - 5, p[1], "open"));
        }
    }
}
