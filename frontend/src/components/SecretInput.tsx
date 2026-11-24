import { useState } from "react";
import type { ComponentProps } from "react";
import { Eye, EyeOff } from "lucide-react";

/** A password or key field with an eye to show what was typed, so a typo is seen before it is sent. */
export default function SecretInput({ className = "input", disabled, ...rest }: Omit<ComponentProps<"input">, "type">) {
  const [shown, setShown] = useState(false);
  return (
    <span className="secret">
      <input {...rest} className={className} disabled={disabled} type={shown ? "text" : "password"} />
      <button
        type="button"
        className="ib secret-eye"
        onClick={() => setShown(!shown)}
        // Keep the field focused while it is toggled, so typing carries on.
        onMouseDown={(e) => e.preventDefault()}
        aria-label={shown ? "Hide what you typed" : "Show what you typed"}
        aria-pressed={shown}
        title={shown ? "Hide" : "Show"}
        disabled={disabled}
      >
        {shown ? <EyeOff size={14} /> : <Eye size={14} />}
      </button>
    </span>
  );
}
