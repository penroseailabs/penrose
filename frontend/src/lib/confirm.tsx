import ReactDOM from "react-dom/client";
import Sheet from "../components/Sheet";

type Ask = { message: string; detail?: string; action: string };

/** A confirmation drawn by Relay. The browser's own confirm() prefixes the page's address ("relay-companion.….workers.dev says"). */
export function confirmDialog({ message, detail, action }: Ask): Promise<boolean> {
  return new Promise((resolve) => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = ReactDOM.createRoot(host);
    const done = (ok: boolean) => {
      root.unmount();
      host.remove();
      resolve(ok);
    };
    root.render(
      <Sheet title={message} onClose={() => done(false)} className="confirm">
        {detail && <p className="sheet-lede">{detail}</p>}
        <div className="card-foot">
          <button className="btn" style={{ marginLeft: "auto" }} onClick={() => done(false)}>
            Cancel
          </button>
          <button className="btn primary" autoFocus onClick={() => done(true)}>
            {action}
          </button>
        </div>
      </Sheet>,
    );
  });
}
