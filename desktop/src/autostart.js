// Start penrose when the user logs in (which is also what a reboot or a fresh boot comes to), on every platform.
//   Windows: a value in HKCU\...\Run, through Electron's login-item API. The installer writes the same value
//            (desktop/build/installer.nsh), so the first start after installing agrees with it.
//   macOS:   a login item, through the same API.
//   Linux:   Electron has no API for it, so an XDG autostart entry, ~/.config/autostart/penrose.desktop. The build is
//            an AppImage, which runs from a mount that goes away with it, so the entry points at the AppImage file.
// At login penrose starts with --hidden: in the tray, window closed, like after the user closes the window.

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const HIDDEN_ARG = "--hidden";
const NAME = "penrose"; // the Run value's name; the installer uses the same one

function desktopEntryPath(env = process.env, home = os.homedir()) {
  return path.join(env.XDG_CONFIG_HOME || path.join(home, ".config"), "autostart", "penrose.desktop");
}

/** A Desktop Entry value: backslash and double quote escaped, and quoted when it has anything but plain characters. */
function execArg(value) {
  if (/^[A-Za-z0-9_\-./:+=@,]+$/.test(value)) return value;
  return `"${value.replace(/[\\"`$]/g, "\\$&").replace(/%/g, "%%")}"`;
}

/** The contents of the Linux autostart entry for a program path. */
function desktopEntry(execPath) {
  return [
    "[Desktop Entry]",
    "Type=Application",
    `Name=${NAME}`,
    "Comment=Starts penrose when you log in",
    `Exec=${execArg(execPath)} ${HIDDEN_ARG}`,
    "Terminal=false",
    "X-GNOME-Autostart-enabled=true",
    "",
  ].join("\n");
}

/** What to start on Linux: the AppImage file if that is how penrose runs, else the program itself. */
function linuxTarget(env = process.env, execPath = process.execPath) {
  return env.APPIMAGE || execPath;
}

/** Turns start-at-login on or off. Returns whether it is now on. */
function set(app, enabled) {
  if (process.platform === "linux") {
    const file = desktopEntryPath();
    if (enabled) {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, desktopEntry(linuxTarget()));
    } else {
      fs.rmSync(file, { force: true });
    }
    return enabled;
  }
  if (process.platform === "win32") {
    app.setLoginItemSettings({ openAtLogin: enabled, name: NAME, args: enabled ? [HIDDEN_ARG] : [] });
  } else {
    app.setLoginItemSettings({ openAtLogin: enabled });
  }
  return enabled;
}

/** Whether it is on now. Windows also counts the user switching it off in Task Manager's Startup apps. */
function isOn(app) {
  if (process.platform === "linux") {
    try {
      return fs.readFileSync(desktopEntryPath(), "utf8").includes("Exec=");
    } catch {
      return false;
    }
  }
  const settings = app.getLoginItemSettings(process.platform === "win32" ? { path: process.execPath, args: [HIDDEN_ARG] } : {});
  if (process.platform === "win32") return Boolean(settings.executableWillLaunchAtLogin);
  return Boolean(settings.openAtLogin);
}

/** Whether this start came from logging in (the argument is ours; macOS reports it itself). */
function startedAtLogin(app, argv = process.argv) {
  if (argv.includes(HIDDEN_ARG)) return true;
  if (process.platform === "darwin") return Boolean(app.getLoginItemSettings().wasOpenedAtLogin);
  return false;
}

/** Makes sure the entry points at this copy of penrose (it moves when the AppImage does, or the install folder
 *  changes). A user's choice to switch it off is kept. */
function refresh(app) {
  if (isOn(app)) set(app, true);
}

module.exports = { HIDDEN_ARG, desktopEntry, desktopEntryPath, execArg, isOn, linuxTarget, refresh, set, startedAtLogin };
