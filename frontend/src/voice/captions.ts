/**
 * Captions for Relay's voice (desk.ts): the reply's words shown as they are heard, not as their text arrives.
 *
 * The Realtime API streams a reply's text well ahead of its audio and gives no word timings. So the words are paced
 * by syllables at a speaking rate, and pinned to the audio at its pauses: the player finds the short silences in
 * the sound it plays (a full stop, a comma), and each one is matched to the punctuation in the text nearest where
 * the pacing has got to. The rate is learnt from those matches, so drift never builds up past one clause. At a
 * sentence's end the caption waits for the pause rather than running on into the next sentence.
 */

/** A silence in the reply's audio, in seconds from its start. */
export type Pause = { start: number; end: number };

type Word = { end: number; weight: number; sentence: boolean; clause: boolean };

const DEFAULT_RATE = 4.6; // syllables a second, at speed 1
const MIN_RATE = 2.5;
const MAX_RATE = 8;
const HOLD = 0.7; // seconds a sentence's end waits for its pause before running on
const SENTENCE_END = /[.!?…。！？]["'”’)\]]*$/;
const CLAUSE_END = /[,;:—–、，]["'”’)\]]*$/;

/** About how long a word takes to say, in syllables. Rough by design: the pauses correct it. */
export function syllables(word: string): number {
  const lower = word.toLowerCase();
  const latin = lower.replace(/[^a-z]/g, "");
  let n = 0;
  if (latin) {
    n = (latin.match(/[aeiouy]+/g) || []).length;
    if (latin.length > 2 && /[^aeiouy]e$/.test(latin) && !/[^aeiouy]le$/.test(latin)) n--; // "make", not "table"
    n = Math.max(1, n);
  }
  n += (lower.match(/\d/g) || []).length * 1.3; // "8" is one, "forty" two
  n += lower.replace(/[a-z\d\s\p{P}\p{S}]/gu, "").length / 2; // other scripts: about a syllable a character or two
  return Math.max(n, 0.5);
}

function words(text: string): Word[] {
  const out: Word[] = [];
  for (const match of text.matchAll(/\S+/g)) {
    const token = match[0];
    out.push({
      end: match.index! + token.length,
      weight: syllables(token),
      sentence: SENTENCE_END.test(token),
      clause: CLAUSE_END.test(token),
    });
  }
  return out;
}

export class Captioner {
  private text = "";
  private list: Word[] = [];
  private done = false;
  private rate = DEFAULT_RATE;
  private anchorWord = 0; // words before it are pinned to the audio...
  private anchorTime = 0; // ...which reached them here
  private usedPauses = 0;
  private shown = 0; // words on screen; never goes back

  reset() {
    this.text = "";
    this.list = [];
    this.done = false;
    this.anchorWord = 0;
    this.anchorTime = 0;
    this.usedPauses = 0;
    this.shown = 0;
    // The rate is kept: the next reply is in the same voice.
  }

  /** The reply's text so far; `done` once it is whole. */
  setText(text: string, done = false) {
    this.text = text;
    this.list = words(text);
    this.done = done;
  }

  /** The text heard by `t` seconds into the reply, given the pauses the player has found so far. */
  at(t: number, pauses: Pause[]): string {
    for (; this.usedPauses < pauses.length && pauses[this.usedPauses].start <= t; this.usedPauses++) {
      this.pin(pauses[this.usedPauses]);
    }
    // Words whose start the pacing has reached, from the last pin.
    const ready = this.done || /\s$/.test(this.text) ? this.list.length : this.list.length - 1; // the last may be cut
    let budget = (t - this.anchorTime) * this.rate;
    let i = this.anchorWord;
    while (i < ready && budget >= 0) {
      if (i > this.anchorWord && this.list[i - 1].sentence) {
        // A sentence ended: wait for its pause, unless there plainly was none.
        if (budget < HOLD * this.rate) break;
        budget -= HOLD * this.rate;
      }
      budget -= this.list[i].weight;
      i++;
    }
    this.shown = Math.max(this.shown, i);
    return this.shown ? this.text.slice(0, this.list[this.shown - 1].end) : "";
  }

  /** A pause in the audio: the punctuation nearest where the pacing is, if any is near enough, is where it fell. */
  private pin(pause: Pause) {
    if (pause.start - this.anchorTime < 0.25) return; // too soon after the last to be a new clause
    const here = this.syllablesTo(this.position(pause.start)); // how far the pacing alone has got
    let best = -1;
    let bestGap = Infinity;
    let walked = 0; // syllables from the anchor
    for (let i = this.anchorWord; i < this.list.length; i++) {
      walked += this.list[i].weight;
      if ((this.list[i].sentence || this.list[i].clause) && Math.abs(walked - here) < bestGap) {
        best = i + 1;
        bestGap = Math.abs(walked - here);
      }
      if (walked - here > bestGap) break; // every later mark is further still
    }
    if (best < 0) return;
    const span = this.syllablesTo(best);
    // A breath with no punctuation near it is not a clause: leave the pacing be.
    if (bestGap > Math.max(4, 0.4 * span)) return;
    const seconds = pause.start - this.anchorTime;
    if (span > 0 && seconds > 0.3) {
      this.rate = Math.min(MAX_RATE, Math.max(MIN_RATE, 0.6 * this.rate + 0.4 * (span / seconds)));
    }
    this.anchorWord = best;
    this.anchorTime = pause.end;
    this.shown = Math.max(this.shown, best);
  }

  /** The word the pacing alone reaches by `t` (no hold), counted from the anchor. */
  private position(t: number) {
    let budget = (t - this.anchorTime) * this.rate;
    let i = this.anchorWord;
    while (i < this.list.length && budget >= this.list[i].weight / 2) {
      budget -= this.list[i].weight;
      i++;
    }
    return i;
  }

  /** Syllables from the anchor to word `end` (exclusive). */
  private syllablesTo(end: number) {
    let n = 0;
    for (let i = this.anchorWord; i < end && i < this.list.length; i++) n += this.list[i].weight;
    return n;
  }
}
