// The first-launch data folder question for macOS and Linux (src/datafolder.js). Run: npm test (in desktop/).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { askDataFolder, evaluateFolder, hasData, pointerFile } = require("../src/datafolder.js");

const GB = 1024 ** 3;
// Tests that touch real folders check them with this computer's path rules.
const HERE = process.platform === "win32" ? "win32" : "linux";

function linux({ free = 200 * GB, mounts = [], existing = [] } = {}) {
  return {
    platform: "linux",
    mounts: [{ point: "/", type: "ext4", options: ["rw", "relatime"] }, ...mounts],
    tmpDirs: ["/tmp", "/var/tmp"],
    programDir: "/tmp/.mount_penroseX",
    exists: (p) => p === "/" || ["/home", "/home/u", "/home/u/.config", "/media", "/media/u", "/mnt"].includes(p) ||
      existing.some((e) => p === e || e.startsWith(`${p}/`)),
    realpath: (p) => p,
    freeBytes: () => free,
  };
}

function mac({ free = 300 * GB, mounts = [] } = {}) {
  return {
    platform: "darwin",
    mounts: [
      { point: "/", type: "apfs", options: ["sealed", "local", "read-only", "journaled"] },
      { point: "/System/Volumes/Data", type: "apfs", options: ["local", "journaled", "nobrowse"] },
      ...mounts,
    ],
    tmpDirs: ["/private/var/folders/ab/T", "/tmp", "/private/tmp"],
    programDir: "/Applications/penrose.app",
    exists: (p) => !p.endsWith("config.json"), // every folder, no earlier data
    realpath: (p) => p,
    freeBytes: () => free,
  };
}

test("the default folder keeps its name and plenty of space is fine", () => {
  const r = evaluateFolder("/home/u/.config/Relay", linux());
  assert.equal(r.final, "/home/u/.config/Relay");
  assert.equal(r.level, 0);
  assert.deepEqual(r.lines, ["200 GB free on this disk. Enough even if the phone reaches its 64 GB limit."]);
});

test("a pick gets a penrose folder inside it, unless it already is one", () => {
  assert.equal(evaluateFolder("/home/u/Data", linux()).final, "/home/u/Data/penrose");
  assert.equal(evaluateFolder("/home/u/Penrose/", linux()).final, "/home/u/Penrose");
});

test("space levels match the Windows installer", () => {
  assert.equal(evaluateFolder("/home/u", linux({ free: 5 * GB })).level, 2);
  assert.match(evaluateFolder("/home/u", linux({ free: 5 * GB })).lines[0], /^Only 5 GB free on this disk\. penrose needs about 12 GB/);
  assert.match(evaluateFolder("/home/u", linux({ free: 15 * GB })).lines[0], /tight/);
  assert.match(evaluateFolder("/home/u", linux({ free: 40 * GB })).lines[0], /Enough for typical use/);
  assert.match(evaluateFolder("/home/u", linux({ free: null })).lines[0], /Could not read the free space/);
});

test("existing data is used as it is", () => {
  const r = evaluateFolder("/home/u/old", linux({ existing: ["/home/u/old/penrose/config.json"] }));
  assert.match(r.lines[0], /already in this folder/);
});

test("refused: temporary folders, the app itself, network, FAT32, read-only", () => {
  const refused = (pick, env) => {
    const r = evaluateFolder(pick, env);
    assert.equal(r.level, 3, `${pick}: ${r.lines}`);
    return r.lines[0];
  };
  assert.match(refused("/tmp/x", linux()), /temporary folder/);
  assert.match(refused("/private/var/folders/ab/T/x", mac()), /temporary folder/);
  assert.match(refused("/Applications/penrose.app/Contents", mac()), /inside the penrose app/);
  assert.match(refused("/mnt/nas", linux({ mounts: [{ point: "/mnt/nas", type: "nfs4", options: ["rw"] }] })), /Network/);
  assert.match(refused("/Volumes/Share", mac({ mounts: [{ point: "/Volumes/Share", type: "smbfs", options: ["nodev", "nosuid"] }] })), /Network/);
  assert.match(refused("/media/u/USB", linux({ mounts: [{ point: "/media/u/USB", type: "vfat", options: ["rw"] }] })), /^\/media\/u\/USB uses FAT32/);
  assert.match(refused("/Volumes/Stick", mac({ mounts: [{ point: "/Volumes/Stick", type: "msdos", options: ["local", "nodev"] }] })), /FAT32/);
  assert.match(refused("/srv/ro", linux({ mounts: [{ point: "/srv/ro", type: "ext4", options: ["ro"] }] })), /read-only/);
  assert.match(refused("relative/path", linux()), /full path/);
});

test("macOS: the read-only system volume does not refuse the home folder", () => {
  const r = evaluateFolder("/Users/u/Library/Application Support/Relay", mac());
  assert.equal(r.level, 0);
  assert.match(r.lines[0], /on this disk/);
});

test("cautions: removable drives, other filesystems, non-English letters", () => {
  const usb = evaluateFolder("/media/u/Ext", linux({ mounts: [{ point: "/media/u/Ext", type: "exfat", options: ["rw"] }] }));
  assert.equal(usb.level, 1);
  assert.deepEqual(usb.lines.slice(1), [
    "This is a removable drive: penrose cannot start while it is unplugged.",
    "This drive uses exFAT. ext4 is recommended for the phone.",
  ]);
  assert.match(usb.lines[0], /on \/media\/u\/Ext\./);
  const ext = evaluateFolder("/Volumes/Big", mac({ mounts: [{ point: "/Volumes/Big", type: "apfs", options: ["local"] }] }));
  assert.equal(ext.level, 1);
  assert.equal(evaluateFolder("/home/u/Données", linux()).level, 1);
  const low = evaluateFolder("/media/u/Ext", linux({ free: 3 * GB, mounts: [{ point: "/media/u/Ext", type: "ext4", options: ["rw"] }] }));
  assert.equal(low.level, 2, "low space outranks a caution");
});

function fakeDialog(script) {
  const shown = [];
  return {
    shown,
    async showMessageBox(options) {
      shown.push(options);
      const label = script.shift();
      assert.ok(label, `unexpected box: ${options.message}`);
      return { response: options.buttons.indexOf(label) };
    },
    async showOpenDialog(options) {
      shown.push(options);
      const pick = script.shift();
      return pick === null ? { canceled: true, filePaths: [] } : { canceled: false, filePaths: [pick] };
    },
  };
}

test("the question: low space by default, choose another, use it; the choice is saved", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "penrose-ask-"));
  try {
    const appData = path.join(root, "appdata");
    const elsewhere = path.join(root, "big");
    let free = 5 * GB;
    const env = { ...linux(), platform: HERE, tmpDirs: [], exists: (p) => fs.existsSync(p), freeBytes: () => free };
    const dialog = fakeDialog(["Choose another folder…", elsewhere, "Use this folder"]);
    const origOpen = dialog.showOpenDialog;
    dialog.showOpenDialog = async (o) => {
      free = 100 * GB;
      return origOpen(o);
    };
    const final = await askDataFolder({ dialog, appData, defaultDir: path.join(appData, "Relay"), env });
    assert.equal(final, path.join(elsewhere, "penrose"));
    assert.equal(dialog.shown[0].type, "warning");
    assert.equal(dialog.shown[0].buttons[dialog.shown[0].defaultId], "Choose another folder…", "low space suggests another");
    assert.equal(fs.readFileSync(pointerFile(appData), "utf8"), `${final}\n`);
    assert.ok(fs.statSync(final).isDirectory());
    assert.deepEqual(fs.readdirSync(final), [], "the write test leaves nothing behind");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("quitting saves nothing; a folder that cannot be made reopens the chooser", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "penrose-ask-"));
  try {
    const appData = path.join(root, "appdata");
    const env = { ...linux(), platform: HERE, tmpDirs: [], exists: (p) => fs.existsSync(p) };
    assert.equal(await askDataFolder({ dialog: fakeDialog(["Quit"]), appData, defaultDir: path.join(appData, "Relay"), env }), "");
    assert.ok(!fs.existsSync(pointerFile(appData)));

    const blocker = path.join(root, "file");
    fs.writeFileSync(blocker, "");
    const good = path.join(root, "good");
    const dialog = fakeDialog(["Use this folder", "OK", good, "Use this folder"]);
    const final = await askDataFolder({ dialog, appData, defaultDir: blocker, env });
    assert.equal(dialog.shown[1].message, "penrose cannot create its folder here");
    assert.equal(final, path.join(good, "penrose"));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("data from before the question is recognised", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "penrose-old-"));
  try {
    assert.equal(hasData(root), false);
    fs.mkdirSync(path.join(root, "android"));
    assert.equal(hasData(root), true);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
