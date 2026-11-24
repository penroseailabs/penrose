import { useState } from "react";
import { CreditCard, Hash, KeyRound, LockKeyhole, X } from "lucide-react";
import SecretInput from "./SecretInput";

export type PrivateKind = "card" | "password" | "code" | "other";
export type PrivateEntry = { label: string; value: string };
/** What penrose's question asks for (api Run.private_asked). */
export type PrivateAsk = { kind: PrivateKind; label?: string };

type Props = {
  /** Asked for by penrose; without it the user picks what they give. */
  ask?: PrivateAsk;
  onSend: (entries: PrivateEntry[]) => Promise<boolean>;
  busy?: boolean;
  onDone?: () => void;
};

const KINDS: { kind: PrivateKind; name: string; icon: typeof CreditCard }[] = [
  { kind: "card", name: "Card", icon: CreditCard },
  { kind: "password", name: "Password", icon: KeyRound },
  { kind: "code", name: "Code", icon: Hash },
  { kind: "other", name: "Other", icon: LockKeyhole },
];

/** Values given privately: penrose's model learns that they came and their names, never what they are; the phone
 * types them where the task needs them. Each kind gets a form shaped for it: a card, a password with its eye, a code. */
export default function PrivateFields({ ask, onSend, busy, onDone }: Props) {
  const [kind, setKind] = useState<PrivateKind>(ask?.kind || "card");
  const [entries, setEntries] = useState<PrivateEntry[] | null>(null);
  const [round, setRound] = useState(0); // a fresh, empty form after each send

  return (
    <form
      className="ask surface appear private-fields"
      onSubmit={async (e) => {
        e.preventDefault();
        if (!entries) return;
        if (await onSend(entries)) {
          setRound((n) => n + 1);
          setEntries(null);
          onDone?.();
        }
      }}
    >
      <div className="label">
        <LockKeyhole size={12} />
        {ask ? ask.label || "Private answer" : "Send privately"}
        {onDone && (
          <button type="button" className="ib private-close" onClick={onDone} aria-label="Close" title="Close">
            <X size={13} />
          </button>
        )}
      </div>
      {!ask && (
        <div className="private-kinds" role="radiogroup" aria-label="What you are sending">
          {KINDS.map(({ kind: k, name, icon: Icon }) => (
            <button
              key={k}
              type="button"
              role="radio"
              aria-checked={kind === k}
              className={`private-kind ${kind === k ? "on" : ""}`}
              onClick={() => {
                setKind(k);
                setEntries(null);
              }}
            >
              <Icon size={13} /> {name}
            </button>
          ))}
        </div>
      )}
      {kind === "card" && <CardForm key={round} onChange={setEntries} />}
      {kind === "password" && <OneValue key={round} label={ask?.label || "Password"} secret onChange={setEntries} />}
      {kind === "code" && <OneValue key={round} label={ask?.label || "Code"} code onChange={setEntries} />}
      {kind === "other" && <OneValue key={round} label={ask?.label} secret onChange={setEntries} />}
      <div className="row">
        <span className="grow" />
        <button className="btn primary" disabled={!entries || busy}>
          Send privately
        </button>
      </div>
      <small className="ask-note">The AI model sees only what each value is, never the value. It is typed straight into the phone and forgotten when the task ends.</small>
    </form>
  );
}

// --- a card -------------------------------------------------------------------------------------------------------

function brandOf(digits: string) {
  if (/^3[47]/.test(digits)) return "Amex";
  if (/^4/.test(digits)) return "Visa";
  if (/^(5[1-5]|2[2-7])/.test(digits)) return "Mastercard";
  if (/^(60|65|81|82|508)/.test(digits)) return /^(6011|65)/.test(digits) ? "Discover" : "RuPay";
  if (/^35/.test(digits)) return "JCB";
  if (/^3(0|6|8)/.test(digits)) return "Diners";
  return "";
}

/** The card number as printed: 4-4-4-4, Amex 4-6-5. */
function grouped(digits: string) {
  if (/^3[47]/.test(digits)) return [digits.slice(0, 4), digits.slice(4, 10), digits.slice(10, 15)].filter(Boolean).join(" ");
  return digits.replace(/(\d{4})(?=\d)/g, "$1 ");
}

function luhn(digits: string) {
  let sum = 0;
  for (let i = 0; i < digits.length; i++) {
    let d = Number(digits[digits.length - 1 - i]);
    if (i % 2) d = d * 2 > 9 ? d * 2 - 9 : d * 2;
    sum += d;
  }
  return sum % 10 === 0;
}

function CardForm({ onChange }: { onChange: (entries: PrivateEntry[] | null) => void }) {
  const [number, setNumber] = useState("");
  const [expiry, setExpiry] = useState("");
  const [cvv, setCvv] = useState("");
  const [name, setName] = useState("");
  const brand = brandOf(number);
  const amex = brand === "Amex";
  const numberOk = number.length >= 12 && number.length <= 19 && luhn(number);
  const month = Number(expiry.slice(0, 2));
  const expiryOk = /^\d{2}\/\d{2}$/.test(expiry) && month >= 1 && month <= 12;
  const cvvOk = amex ? cvv.length === 4 : cvv.length === 3 || cvv.length === 4;
  const nameOk = !name || /^[\x20-\x7e]+$/.test(name);

  function update(next: { number?: string; expiry?: string; cvv?: string; name?: string }) {
    const v = { number, expiry, cvv, name, ...next };
    const good =
      v.number.length >= 12 && luhn(v.number) && /^(0[1-9]|1[0-2])\/\d{2}$/.test(v.expiry) &&
      /^\d{3,4}$/.test(v.cvv) && (!v.name || /^[\x20-\x7e]+$/.test(v.name));
    onChange(
      good
        ? [
            { label: "Card number", value: v.number },
            { label: "Card expiry (MM/YY)", value: v.expiry },
            { label: "Card CVV", value: v.cvv },
            ...(v.name.trim() ? [{ label: "Name on card", value: v.name.trim() }] : []),
          ]
        : null,
    );
  }

  return (
    <div className="pcard" role="group" aria-label="Card">
      <div className="pcard-top">
        <span className="pcard-chip" aria-hidden="true" />
        <span className="pcard-brand">{brand}</span>
      </div>
      <input
        className={`pcard-number ${number.length >= 12 && !numberOk ? "bad" : ""}`}
        aria-label="Card number"
        inputMode="numeric"
        autoComplete="cc-number"
        placeholder="0000 0000 0000 0000"
        value={grouped(number)}
        onChange={(e) => {
          const digits = e.target.value.replace(/\D/g, "").slice(0, 19);
          setNumber(digits);
          update({ number: digits });
        }}
        autoFocus
      />
      <div className="pcard-row">
        <label className="pcard-small">
          <span>Expires</span>
          <input
            className={expiry.length === 5 && !expiryOk ? "bad" : ""}
            aria-label="Expiry, month and year"
            inputMode="numeric"
            autoComplete="cc-exp"
            placeholder="MM/YY"
            value={expiry}
            onChange={(e) => {
              const d = e.target.value.replace(/\D/g, "").slice(0, 4);
              const v = d.length > 2 ? `${d.slice(0, 2)}/${d.slice(2)}` : d;
              setExpiry(v);
              update({ expiry: v });
            }}
          />
        </label>
        <label className="pcard-small pcard-cvv">
          <span>{amex ? "CID" : "CVV"}</span>
          <SecretInput
            className={`input ${cvv.length >= 3 && !cvvOk ? "bad" : ""}`}
            aria-label="Security code"
            inputMode="numeric"
            autoComplete="cc-csc"
            placeholder={amex ? "4 digits" : "3 digits"}
            value={cvv}
            onChange={(e) => {
              const d = e.target.value.replace(/\D/g, "").slice(0, 4);
              setCvv(d);
              update({ cvv: d });
            }}
          />
        </label>
      </div>
      <input
        className={`pcard-name ${nameOk ? "" : "bad"}`}
        aria-label="Name on card (optional)"
        autoComplete="cc-name"
        placeholder="NAME ON CARD (optional)"
        value={name}
        onChange={(e) => {
          setName(e.target.value);
          update({ name: e.target.value });
        }}
      />
      {number.length >= 12 && !numberOk && <small className="pcard-hint">That number doesn't look right. Check the digits.</small>}
    </div>
  );
}

// --- one value: a password (with its eye), a code, anything else ---------------------------------------------------

function OneValue({ label, secret, code, onChange }: {
  label?: string;
  secret?: boolean;
  code?: boolean;
  onChange: (entries: PrivateEntry[] | null) => void;
}) {
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const named = label || name.trim();
  const ascii = /^[\x20-\x7e]*$/.test(value);
  const update = (n: string, v: string) => {
    const what = label || n.trim();
    onChange(what && v && /^[\x20-\x7e]+$/.test(v) ? [{ label: what, value: v }] : null);
  };

  return (
    <div className="private-one">
      {!label && (
        <input
          className="input"
          aria-label="What it is"
          placeholder="What it is: passport number, PIN…"
          maxLength={60}
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            update(e.target.value, value);
          }}
          autoFocus
        />
      )}
      {code ? (
        <input
          className="input private-code"
          aria-label={named || "Code"}
          inputMode="numeric"
          autoComplete="one-time-code"
          placeholder="Code"
          maxLength={12}
          value={value}
          onChange={(e) => {
            const v = e.target.value.replace(/\s/g, "");
            setValue(v);
            update(name, v);
          }}
          autoFocus
        />
      ) : (
        <SecretInput
          aria-label={named || "Value"}
          autoComplete={secret && label === "Password" ? "current-password" : "off"}
          placeholder={label || "Value"}
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            update(name, e.target.value);
          }}
          autoFocus={!!label}
        />
      )}
      {!ascii && <small className="pcard-hint">Letters, digits and symbols only: no accents or emoji.</small>}
    </div>
  );
}
