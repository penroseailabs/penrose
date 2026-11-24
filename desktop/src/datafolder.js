// Where penrose keeps its data on macOS and Linux. Those builds (dmg, zip, AppImage) have no install step, so the
// first start asks, with the same checks and wording as the Windows installer's page (desktop/build/installer.nsh).
// The answer goes to penrose/data-location.txt in the per-user app data folder, which main.js (chosenDataDir) and
// backend/app/cli.py read on every platform.

const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const FOLDER = "penrose";
const NEED_GB = 12; // a typical phone
const ROOMY_GB = 20;
const LIMIT_GB = 64; // the phone's disk limit
const NETWORK = new Set(["nfs", "nfs4", "cifs", "smb3", "smbfs", "afpfs", "webdav", "sshfs", "fuse.sshfs", "davfs",
  "fuse.davfs2", "ceph", "glusterfs", "fuse.glusterfs", "afs", "fuse.rclone", "9p"]);
const FAT = new Set(["vfat", "msdos", "fat", "fat32"]);
const FS_NAMES = { exfat: "exFAT", ntfs: "NTFS", ntfs3: "NTFS", fuseblk: "NTFS", hfs: "Mac OS Extended" };

const CHOOSE = "Choose another folder…";
const USE = "Use this folder";
const QUIT = "Quit";

function pointerFile(appData) {
  return path.join(appData, FOLDER, "data-location.txt");
}

/** True when this folder holds penrose data already: an install from before the question existed keeps it. */
function hasData(dir) {
  return ["config.json", "android", "data"].some((name) => fs.existsSync(path.join(dir, name)));
}

/** The mounted filesystems: { point, type, options }. */
function readMounts(platform = process.platform) {
  try {
    if (platform === "linux") {
      return fs.readFileSync("/proc/mounts", "utf8").split("\n").filter(Boolean).map((line) => {
        const [, point, type, options] = line.split(" ");
        const unescape = (text) => text.replace(/\\([0-7]{3})/g, (_, octal) => String.fromCharCode(parseInt(octal, 8)));
        return { point: unescape(point), type, options: (options || "").split(",") };
      });
    }
    if (platform === "darwin") {
      return execFileSync("/sbin/mount", { encoding: "utf8", timeout: 5000 }).split("\n")
        .map((line) => line.match(/^.+? on (.+) \(([^,)]+)(?:, ([^)]*))?\)$/))
        .filter(Boolean)
        .map((m) => ({ point: m[1], type: m[2], options: (m[3] || "").split(", ") }));
    }
  } catch {}
  return [];
}

/** The real system, for evaluateFolder; tests pass their own. */
function systemEnv() {
  const resources = process.resourcesPath || "";
  const bundle = resources.match(/^(.*?\.app)(\/|$)/);
  return {
    platform: process.platform,
    mounts: readMounts(),
    tmpDirs: [os.tmpdir(), "/tmp", "/var/tmp", "/private/tmp", "/private/var/folders"],
    programDir: bundle ? bundle[1] : resources ? path.dirname(resources) : "",
    exists: (p) => fs.existsSync(p),
    realpath: (p) => {
      try {
        return fs.realpathSync(p);
      } catch {
        return p;
      }
    },
    freeBytes: (p) => {
      try {
        const stat = fs.statfsSync(p);
        return stat.bavail * stat.bsize;
      } catch {
        return null;
      }
    },
  };
}

/** Path rules for the platform being checked (macOS and Linux in the app; the tests also run on Windows). */
function pathsFor(env) {
  return env.platform === "win32" ? path.win32 : path.posix;
}

function inside(child, parent, env) {
  if (!parent) return false;
  const rel = pathsFor(env).relative(parent, child);
  return rel === "" || (!rel.startsWith("..") && !pathsFor(env).isAbsolute(rel));
}

/** The nearest folder of `dir` that exists (the pick may be a new folder). */
function existingAncestor(dir, env) {
  const { dirname } = pathsFor(env);
  let at = dir;
  while (!env.exists(at) && dirname(at) !== at) at = dirname(at);
  return at;
}

/** Checks a picked folder. level 0 fine, 1 caution, 2 low on space (allowed, not suggested), 3 refused. */
function evaluateFolder(pick, env = systemEnv()) {
  const path = pathsFor(env);
  const result = { final: "", level: 0, lines: [], freeGb: null };
  const refuse = (text) => Object.assign(result, { level: 3, lines: [text] });
  const caution = (text) => {
    result.level = Math.max(result.level, 1);
    result.lines.push(text);
  };
  if (!pick || !path.isAbsolute(pick)) return refuse("Choose a full path, such as /Volumes/Data.");
  const clean = path.resolve(pick);
  // Everything goes in one folder of its own, unless the pick already is one.
  result.final = ["penrose", "relay"].includes(path.basename(clean).toLowerCase()) ? clean : path.join(clean, FOLDER);

  const base = existingAncestor(result.final, env);
  const real = path.join(env.realpath(base), path.relative(base, result.final));
  if (env.tmpDirs.some((dir) => inside(real, env.realpath(dir), env) || inside(result.final, dir, env))) {
    return refuse("This is a temporary folder, which the system empties. Choose another one.");
  }
  if (env.programDir && (inside(result.final, env.programDir, env) || inside(real, env.programDir, env))) {
    return refuse("This is inside the penrose app, which updates replace. Choose another one.");
  }

  // The drive: the mount with the longest matching mount point.
  const mount = env.mounts.filter((m) => inside(real, m.point, env)).sort((a, b) => b.point.length - a.point.length)[0];
  const type = (mount?.type || "").toLowerCase();
  const where = !mount || mount.point === "/" || mount.point === "/System/Volumes/Data" ? "this disk" : mount.point;
  if (mount && (NETWORK.has(type) || (env.platform === "darwin" && !mount.options.includes("local")))) {
    return refuse("Network folders are too slow for the phone. Choose a folder on this computer.");
  }
  if (FAT.has(type)) {
    return refuse(`${where === "this disk" ? "This disk" : where} uses FAT32, which cannot hold files over 4 GB. The phone's files are larger.`);
  }
  // macOS mounts its system volume read-only and writes through to the data volume, so only Linux reads this flag;
  // saving tries a real write anyway.
  if (env.platform === "linux" && mount?.options.includes("ro")) return refuse("This drive is read-only. Choose another one.");

  // The space.
  const free = env.freeBytes(base);
  result.freeGb = free == null ? null : Math.floor(free / 1024 ** 3);
  const gb = result.freeGb;
  if (env.exists(path.join(result.final, "config.json"))) {
    result.lines.push(`penrose data is already in this folder and will be used as it is.${gb == null ? "" : ` ${gb} GB free.`}`);
  } else if (gb == null) {
    result.lines.push(`Could not read the free space on ${where}. penrose needs about ${NEED_GB} GB.`);
  } else if (gb < NEED_GB) {
    result.level = 2;
    result.lines.push(`Only ${gb} GB free on ${where}. penrose needs about ${NEED_GB} GB, so setup may not finish. Choose another drive or free some space.`);
  } else if (gb < ROOMY_GB) {
    result.lines.push(`${gb} GB free on ${where}. Enough to start, but tight: the phone grows as you add apps.`);
  } else if (gb < LIMIT_GB) {
    result.lines.push(`${gb} GB free on ${where}. Enough for typical use.`);
  } else {
    result.lines.push(`${gb} GB free on ${where}. Enough even if the phone reaches its ${LIMIT_GB} GB limit.`);
  }

  const point = mount?.point || "";
  const removable = env.platform === "darwin" ? point.startsWith("/Volumes/")
    : ["/media/", "/run/media/", "/mnt/"].some((prefix) => point.startsWith(prefix));
  if (removable) caution("This is a removable drive: penrose cannot start while it is unplugged.");
  if (FS_NAMES[type]) caution(`This drive uses ${FS_NAMES[type]}. ${env.platform === "darwin" ? "APFS" : "ext4"} is recommended for the phone.`);
  // The Android emulator can fail on paths with non-English letters.
  if (/[^\x00-\x7f]/.test(result.final)) caution("This path has non-English letters, which the Android emulator can fail on.");
  return result;
}

/** Creates the folder, proves it takes a file, and saves the choice. Throws when it cannot. */
function saveChoice(final, appData) {
  fs.mkdirSync(final, { recursive: true });
  const probe = path.join(final, ".penrose-write-test");
  fs.writeFileSync(probe, "");
  fs.rmSync(probe, { force: true });
  const pointer = pointerFile(appData);
  fs.mkdirSync(path.dirname(pointer), { recursive: true });
  fs.writeFileSync(pointer, `${final}\n`, "utf8");
}

/** Asks where the data goes, until the user settles on a usable folder (returned) or quits (""). */
async function askDataFolder({ dialog, appData, defaultDir, env = systemEnv() }) {
  let pick = defaultDir;
  let choose = false;
  for (;;) {
    if (choose) {
      choose = false;
      const { canceled, filePaths } = await dialog.showOpenDialog({
        title: "Choose where penrose keeps its data",
        buttonLabel: "Choose",
        defaultPath: existingAncestor(pick, env),
        properties: ["openDirectory", "createDirectory"],
      });
      if (!canceled && filePaths?.[0]) pick = filePaths[0];
    }
    const result = evaluateFolder(pick, env);
    const buttons = result.level >= 3 ? [CHOOSE, QUIT] : [USE, CHOOSE, QUIT];
    const { response } = await dialog.showMessageBox({
      type: ["info", "warning", "warning", "error"][result.level],
      title: "penrose",
      message: "Where should penrose keep its data?",
      detail: `Your phone, chats and settings go in one folder. penrose needs about ${NEED_GB} GB, and the phone can grow to ${LIMIT_GB} GB.\n\n${result.final || pick}\n${result.lines.join("\n")}`,
      buttons,
      defaultId: result.level >= 2 ? buttons.indexOf(CHOOSE) : 0,
      cancelId: buttons.indexOf(QUIT),
      noLink: true,
    });
    const choice = buttons[response];
    if (choice === QUIT) return "";
    if (choice === CHOOSE) {
      choose = true;
      continue;
    }
    try {
      saveChoice(result.final, appData);
      return result.final;
    } catch (error) {
      await dialog.showMessageBox({
        type: "error",
        title: "penrose",
        message: "penrose cannot create its folder here",
        detail: `${result.final}\n\n${error.code === "EACCES" || error.code === "EPERM" ? "You do not have permission to write there." : error.message}\n\nChoose another folder.`,
        buttons: ["OK"],
        noLink: true,
      });
      choose = true;
    }
  }
}

module.exports = { askDataFolder, evaluateFolder, hasData, pointerFile, readMounts, saveChoice };
