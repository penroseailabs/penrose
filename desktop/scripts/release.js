// One build ID for everything that ships together: the installer, the bundled services and UI, and the
// phone-side pieces in assets/android. It is the UTC time the build started, yyyymmddhhmmss, so any two builds
// order by it. The installer's version is derived from it, because electron-updater compares semver and
// Windows keeps file versions in four 16-bit numbers: YYYY.MDD.HMM as plain integers (2026.1023.1430,
// 2026.105.905), which sorts numerically in date order and fits both.
//
// `RELAY_BUILD_ID` pins it (a CI run stamping several platforms with one ID); otherwise it is now.

function pad(n) {
  return String(n).padStart(2, "0");
}

/** The build ID, yyyymmddhhmmss in UTC. */
function buildId(date = new Date()) {
  const id = process.env.RELAY_BUILD_ID || "";
  if (id) {
    if (!/^\d{14}$/.test(id)) throw new Error(`RELAY_BUILD_ID must be yyyymmddhhmmss, got "${id}"`);
    return id;
  }
  return (
    date.getUTCFullYear() + pad(date.getUTCMonth() + 1) + pad(date.getUTCDate()) +
    pad(date.getUTCHours()) + pad(date.getUTCMinutes()) + pad(date.getUTCSeconds())
  );
}

/** The semver an installer carries for build `id`: YYYY.MDD.HMM, each a plain integer. */
function versionOf(id) {
  const [year, month, day, hour, minute] = [id.slice(0, 4), id.slice(4, 6), id.slice(6, 8), id.slice(8, 10), id.slice(10, 12)];
  return `${Number(year)}.${Number(month + day)}.${Number(hour + minute)}`;
}

/** The build ID a version was derived from, to the minute ("20261023143000" for 2026.1023.1430). */
function buildIdOf(version) {
  const [year, mdd, hmm] = version.split(".").map(Number);
  const [month, day] = [Math.floor(mdd / 100), mdd % 100];
  const [hour, minute] = [Math.floor(hmm / 100), hmm % 100];
  return `${year}${pad(month)}${pad(day)}${pad(hour)}${pad(minute)}00`;
}

function release() {
  const id = buildId();
  return { buildId: id, version: versionOf(id) };
}

module.exports = { buildId, versionOf, buildIdOf, release };

if (require.main === module) {
  const { buildId: id, version } = release();
  console.log(`build ${id} -> version ${version}`);
}
