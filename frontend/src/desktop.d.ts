/** What the desktop app's preload script exposes to the window (absent in a plain browser). */

type ProviderId = "deepseek" | "openai" | "anthropic";

/** One line of the phone setup's progress. */
type AndroidEvent =
  | { event: "step"; id: string; title: string }
  /** `doing`: the part of a long step under way (the image build's boot, root, Chrome check...). */
  | { event: "progress"; id: string; percent: number; doing?: string }
  | { event: "log"; text: string }
  | { event: "done" }
  | { event: "error"; message: string; hint?: string }
  /** Setup failed and the doctor is running it again by itself; `reply` says why, in plain words. */
  | { event: "retrying"; reply: string }
  /** The run ended (after the phone was started, when it worked). */
  | ({ event: "finished" } & SetupResult);

type SetupResult = { ok: boolean; error?: string; hint?: string; doctor?: DoctorAnswer };

/** The setup doctor's answer (backend/app/provision/doctor.py): plain words and one action from a fixed menu. */
type DoctorAnswer = {
  reply: string;
  state: "working" | "slow" | "stuck" | "failed" | "needs_you" | "done";
  action: "none" | "try_again" | "start_over" | "user_step";
};
type DoctorMessage = { role: "user" | "doctor"; text: string };

/** One line of the companion setup's progress (desktop/src/companion.js). */
type CompanionEvent =
  | { event: "step"; text: string }
  /** The Cloudflare sign-in page was opened in the browser. */
  | { event: "login"; url: string }
  | { event: "done"; origin: string }
  | { event: "error"; message: string };

interface Window {
  relay?: {
    platform?: string;
    // first run
    /** What setup still needs, why the phone did not come up this launch (if it didn't), and what its
     *  post-boot check is doing right now (null once the phone is ready). */
    setupState?: () => Promise<{
      keys: boolean;
      phone: boolean;
      phoneError: { message: string; hint: string } | null;
      phoneStep: string | null;
    }>;
    /** Each provider's saved key, as its last four characters ("…abcd"), or "". */
    keys?: () => Promise<Record<ProviderId, string>>;
    /** Checks the key with its provider, then saves it (an empty key removes it). */
    saveKey?: (provider: ProviderId, key: string) => Promise<{ ok: boolean; error?: string }>;
    androidInfo?: () => Promise<{ existingSdk: string; useExisting: boolean; freeGb: number | null }>;
    /** Starts the phone setup (or joins the run already going); resolves when it ends. */
    setupAndroid?: (options: { acceptLicenses: boolean; useExisting: boolean; startOver?: boolean }) => Promise<SetupResult>;
    /** Asks the setup doctor; `messages` is the conversation so far, which only the window keeps. */
    askSetup?: (messages: DoctorMessage[]) => Promise<DoctorAnswer>;
    /** The setup run so far: its events to replay while it runs, or how the last run ended. */
    androidProgress?: () => Promise<{ running: boolean; events: AndroidEvent[]; result: SetupResult | null }>;
    onAndroidEvent?: (callback: (event: AndroidEvent) => void) => () => void;
    // the app
    /** The app menu asks the window to open one of its sheets. */
    onOpen?: (callback: (what: "models") => void) => () => void;
    onPhoneError?: (callback: (error: { message: string; hint: string }) => void) => () => void;
    /** The post-boot check's current step ("Updating your phone…"), or null when it has finished. */
    onPhoneStep?: (callback: (step: string | null) => void) => () => void;
    setTheme?: (choice: "system" | "light" | "dark") => void;
    openLogs?: () => void;
    /** Shows the exported Cowork skill (android-phone.zip) in the file manager; false when it is not there. */
    revealClaudeSkill?: () => Promise<boolean>;
    /** Whether the anonymous usage count is on (a random ID, the version and the OS, once a day). */
    usageCount?: () => Promise<boolean>;
    setUsageCount?: (on: boolean) => Promise<void>;
    /** The OS's location settings (Windows, macOS), for when location is off for Relay. */
    openLocationSettings?: () => void;
    resetPhone?: () => Promise<boolean>;
    /** Deploys the coordinator and companion app to the user's Cloudflare account (signing in first when
     *  needed) and resolves with the address; joins a run already going. */
    companionSetup?: () => Promise<{ ok: boolean; origin?: string; error?: string }>;
    companionState?: () => Promise<{ running: boolean; step: string; deployed: { version: string; origin: string } | null }>;
    onCompanionEvent?: (callback: (event: CompanionEvent) => void) => () => void;
    /** A native confirmation; true when the user chooses `action`. */
    confirm?: (options: { message: string; detail?: string; action: string }) => Promise<boolean>;
    /** A native popup menu at the pointer; resolves the chosen item's id, or null. */
    menu?: (items: ({ id: string; label: string; enabled?: boolean } | "-")[]) => Promise<string | null>;
  };
}
