// The window's only way to reach the main process.
const { contextBridge, ipcRenderer } = require("electron");

/** Subscribe to a message from the main process; returns the unsubscribe function. */
function listen(channel, callback) {
  const handler = (_event, data) => callback(data);
  ipcRenderer.on(channel, handler);
  return () => ipcRenderer.removeListener(channel, handler);
}

contextBridge.exposeInMainWorld("relay", {
  platform: process.platform,
  // first run: API keys, then the phone
  setupState: () => ipcRenderer.invoke("setup:state"),
  keys: () => ipcRenderer.invoke("keys:get"),
  saveKey: (provider, key) => ipcRenderer.invoke("keys:save", provider, key),
  androidInfo: () => ipcRenderer.invoke("android:info"),
  setupAndroid: (options) => ipcRenderer.invoke("android:setup", options),
  androidProgress: () => ipcRenderer.invoke("android:progress"),
  askSetup: (messages) => ipcRenderer.invoke("setup:ask", messages),
  onAndroidEvent: (callback) => listen("android:event", callback),
  // the app
  onOpen: (callback) => listen("app:open", callback),
  onPhoneError: (callback) => listen("phone:error", callback),
  onPhoneStep: (callback) => listen("phone:step", callback),
  setTheme: (choice) => ipcRenderer.invoke("theme:set", choice),
  // Linux desktops have no one settings page to open.
  openLocationSettings: process.platform === "linux" ? undefined : () => ipcRenderer.invoke("open:location-settings"),
  resetPhone: () => ipcRenderer.invoke("phone:reset"),
  confirm: (options) => ipcRenderer.invoke("dialog:confirm", options),
  menu: (items) => ipcRenderer.invoke("menu:popup", items),
  openLogs: () => ipcRenderer.invoke("open:logs"),
  revealClaudeSkill: () => ipcRenderer.invoke("reveal:claude-skill"),
  // the anonymous usage count (main.js): on unless the user turned it off
  usageCount: () => ipcRenderer.invoke("usage:get"),
  setUsageCount: (on) => ipcRenderer.invoke("usage:set", Boolean(on)),
  // Relay on your phone: deploying the coordinator to the user's Cloudflare account
  companionSetup: () => ipcRenderer.invoke("companion:setup"),
  companionState: () => ipcRenderer.invoke("companion:state"),
  onCompanionEvent: (callback) => listen("companion:event", callback),
});
