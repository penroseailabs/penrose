/**
 * A small, dismissible "new version" bar over the bottom of the app. Plain DOM, outside React, so it works even when
 * the app itself is in the middle of something. Update runs `apply` (installs the waiting worker and reloads);
 * the cross closes it until the next launch.
 */
export function showUpdateBanner(apply: () => void) {
  if (document.getElementById("update-banner")) return;
  const bar = document.createElement("div");
  bar.id = "update-banner";
  bar.setAttribute("role", "status");
  bar.style.cssText = [
    "position:fixed",
    "left:12px",
    "right:12px",
    "bottom:calc(12px + env(safe-area-inset-bottom, 0px))",
    "z-index:2147483000",
    "display:flex",
    "align-items:center",
    "gap:8px",
    "padding:8px 8px 8px 14px",
    "border-radius:var(--r-4, 16px)",
    "background:var(--elevated, #fff)",
    "color:var(--ink, #1d1d1f)",
    "border:1px solid var(--rule, rgba(0,0,0,.08))",
    "box-shadow:0 6px 24px rgba(0,0,0,.18)",
    "font:var(--fs-3, 13px) var(--font, system-ui, sans-serif)",
  ].join(";");

  const text = document.createElement("span");
  text.textContent = "A new version is ready.";
  text.style.flex = "1";

  const update = document.createElement("button");
  update.textContent = "Update";
  update.style.cssText =
    "border:0;border-radius:var(--r-3,12px);padding:8px 14px;font:inherit;font-weight:600;cursor:pointer;" +
    "background:var(--accent,#b05a39);color:#fff";
  update.onclick = () => {
    update.disabled = true;
    update.textContent = "Updating…";
    apply();
  };

  const close = document.createElement("button");
  close.textContent = "✕";
  close.setAttribute("aria-label", "Dismiss");
  close.style.cssText =
    "border:0;background:transparent;color:var(--ink-2,#636369);padding:8px 10px;font:inherit;cursor:pointer";
  close.onclick = () => bar.remove();

  bar.append(text, update, close);
  document.body.append(bar);
}
