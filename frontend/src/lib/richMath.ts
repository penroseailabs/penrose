// Math in replies (KaTeX), loaded the first time a reply holds some (lib/richPlugins.ts). Only `$$…$$` is math:
// a single `$` stays a dollar sign, so prices are never read as formulas.
import "katex/dist/katex.min.css";

export { math } from "@streamdown/math";
