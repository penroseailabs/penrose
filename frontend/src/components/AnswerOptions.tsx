import type { AnswerOption } from "../api";

type Props = {
  options: AnswerOption[];
  disabled?: boolean;
  /** The user chose one: its `message` is what goes out as their own words. */
  onPick: (message: string) => void;
};

/**
 * The answers penrose's question offers (ask_user's `options`), each a button. A button shows its label and, when
 * the message it sends says more than the label, that message too: a tap sends it as the user's own instruction, so
 * they read it first, on a phone as well, where there is no tooltip.
 */
export default function AnswerOptions({ options, disabled, onPick }: Props) {
  return (
    <div className="answer-options appear" role="group" aria-label="Answer with one tap">
      {options.map((option, index) => {
        const message = option.message.trim();
        return (
          <button key={index} className="answer-option" disabled={disabled} onClick={() => onPick(option.message)}>
            <b>{option.label}</b>
            {message !== option.label.trim() && <span>{message}</span>}
          </button>
        );
      })}
    </div>
  );
}
