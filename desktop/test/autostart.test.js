// Start at login (src/autostart.js). Run: npm test (in desktop/).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const autostart = require("../src/autostart.js");

test("the Linux entry runs the program hidden", () => {
  const entry = autostart.desktopEntry("/home/u/Apps/penrose.AppImage");
  assert.match(entry, /^\[Desktop Entry\]\nType=Application\n/);
  assert.match(entry, /\nExec=\/home\/u\/Apps\/penrose\.AppImage --hidden\n/);
  assert.match(entry, /\nX-GNOME-Autostart-enabled=true\n/);
});

test("a path with spaces or special characters is quoted for Exec", () => {
  assert.equal(autostart.execArg("/home/u/My Apps/penrose.AppImage"), '"/home/u/My Apps/penrose.AppImage"');
  assert.equal(autostart.execArg('/tmp/a"b'), '"/tmp/a\\"b"');
  assert.equal(autostart.execArg("/tmp/100%"), '"/tmp/100%%"');
});

test("an AppImage is started from its file, not from its temporary mount", () => {
  assert.equal(autostart.linuxTarget({ APPIMAGE: "/home/u/penrose.AppImage" }, "/tmp/.mount_x/penrose"), "/home/u/penrose.AppImage");
  assert.equal(autostart.linuxTarget({}, "/opt/penrose/penrose"), "/opt/penrose/penrose");
});

test("the entry lives in the XDG config folder", () => {
  assert.equal(autostart.desktopEntryPath({ XDG_CONFIG_HOME: "/x/cfg" }, "/home/u"), path.join("/x/cfg", "autostart", "penrose.desktop"));
  assert.equal(autostart.desktopEntryPath({}, "/home/u"), path.join("/home/u", ".config", "autostart", "penrose.desktop"));
});

test("a start with --hidden is a login start", () => {
  assert.equal(autostart.startedAtLogin({}, ["penrose", "--hidden"]), true);
  assert.equal(autostart.startedAtLogin({ getLoginItemSettings: () => ({}) }, ["penrose"]), false);
});

test("Linux: set writes and removes the file; isOn and refresh follow it", { skip: process.platform !== "linux" }, () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "penrose-autostart-"));
  const before = process.env.XDG_CONFIG_HOME;
  process.env.XDG_CONFIG_HOME = dir;
  try {
    assert.equal(autostart.isOn({}), false);
    autostart.refresh({}); // off stays off
    assert.equal(autostart.isOn({}), false);
    autostart.set({}, true);
    assert.equal(autostart.isOn({}), true);
    fs.writeFileSync(autostart.desktopEntryPath(), "[Desktop Entry]\nExec=/old/place --hidden\n");
    autostart.refresh({});
    assert.doesNotMatch(fs.readFileSync(autostart.desktopEntryPath(), "utf8"), /\/old\/place/);
    autostart.set({}, false);
    assert.equal(autostart.isOn({}), false);
  } finally {
    if (before === undefined) delete process.env.XDG_CONFIG_HOME;
    else process.env.XDG_CONFIG_HOME = before;
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("Windows and macOS: set goes through Electron's login item", { skip: process.platform === "linux" }, () => {
  const calls = [];
  const app = { setLoginItemSettings: (o) => calls.push(o), getLoginItemSettings: () => ({ openAtLogin: true, executableWillLaunchAtLogin: true }) };
  autostart.set(app, true);
  autostart.set(app, false);
  if (process.platform === "win32") {
    assert.deepEqual(calls[0], { openAtLogin: true, name: "penrose", args: ["--hidden"] });
    assert.deepEqual(calls[1], { openAtLogin: false, name: "penrose", args: [] });
  } else {
    assert.deepEqual(calls, [{ openAtLogin: true }, { openAtLogin: false }]);
  }
  assert.equal(autostart.isOn(app), true);
});
