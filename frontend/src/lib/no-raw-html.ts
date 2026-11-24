/**
 * Stands in for `rehype-raw` (vite.config.ts aliases it here). Relay never renders raw HTML from a reply: the words
 * can carry what a page on the phone said. Streamdown imports rehype-raw for its defaults all the same, and with it
 * an HTML parser (parse5, about 175 KB). This plugin does nothing, so raw HTML stays text even where a list of
 * plugins names rehype-raw.
 */
export default function rehypeRaw() {}
