// Diagrams and charts in replies (Mermaid), loaded the first time a reply shows one (components/Diagram.tsx).
import mermaid from "mermaid";

/** The theme's chart series colours (--chart-1 to --chart-8 in styles/tokens.css), first to last. */
const SERIES = 8;

/** Mermaid draws with fixed colours, so it takes the theme's own from the page, where the tokens are resolved for
 *  light or dark. "strict" keeps a diagram a picture: no HTML in its labels and no click handlers. Charts take their
 *  series colours from the theme too: pie slices and bar or line series in turn, and a sankey's nodes in the order
 *  they first appear (`sankeyNodes`), which Mermaid otherwise colours from a fixed palette of its own. */
function configure(sankeyNodes: string[] = []) {
  const css = getComputedStyle(document.documentElement);
  const token = (name: string) => css.getPropertyValue(name).trim();
  const series = Array.from({ length: SERIES }, (_, i) => token(`--chart-${i + 1}`));
  const pies = Object.fromEntries(Array.from({ length: 12 }, (_, i) => [`pie${i + 1}`, series[i % SERIES]]));
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    theme: "base",
    fontFamily: token("--font"),
    themeVariables: {
      darkMode: document.documentElement.dataset.theme === "dark",
      background: "transparent",
      fontFamily: token("--font"),
      fontSize: "14px",
      primaryColor: token("--well"),
      secondaryColor: token("--well"),
      tertiaryColor: token("--well"),
      primaryTextColor: token("--ink"),
      primaryBorderColor: token("--ink-3"),
      lineColor: token("--ink-2"),
      edgeLabelBackground: token("--well"),
      ...pies,
      pieOpacity: "1",
      pieStrokeColor: token("--content"),
      pieStrokeWidth: "1.5px",
      pieOuterStrokeColor: "transparent",
      pieOuterStrokeWidth: "0px",
      pieTitleTextSize: "15px",
      pieTitleTextColor: token("--ink"),
      pieSectionTextSize: "13px",
      pieSectionTextColor: token("--on-accent"),
      pieLegendTextSize: "13px",
      pieLegendTextColor: token("--ink-2"),
      xyChart: {
        plotColorPalette: series.join(","),
        backgroundColor: "transparent",
        titleColor: token("--ink"),
        xAxisLabelColor: token("--ink-2"),
        xAxisTitleColor: token("--ink-2"),
        xAxisTickColor: token("--ink-3"),
        xAxisLineColor: token("--ink-3"),
        yAxisLabelColor: token("--ink-2"),
        yAxisTitleColor: token("--ink-2"),
        yAxisTickColor: token("--ink-3"),
        yAxisLineColor: token("--ink-3"),
      },
    },
    sankey: {
      nodeColors: Object.fromEntries(sankeyNodes.map((node, i) => [node, series[i % SERIES]])),
    },
  });
}

/** A sankey's node names in the order Mermaid reads them, or none when `source` is some other diagram. */
async function sankeyNodes(source: string): Promise<string[]> {
  if (!/^\s*sankey/.test(source)) return [];
  const diagram = await mermaid.mermaidAPI.getDiagramFromText(source);
  const db = diagram.db as { getGraph?: () => { nodes: { id: string }[] } };
  return db.getGraph?.().nodes.map((node) => node.id) ?? [];
}

let drawn = 0;

/** The diagram `source` describes, as SVG markup in the current theme. Rejects when the source is not valid. */
export async function renderDiagram(source: string): Promise<string> {
  configure();
  // A sankey is read once to learn its nodes (under the same settings, so the names match), then drawn with them.
  const nodes = await sankeyNodes(source);
  if (nodes.length) configure(nodes);
  const { svg } = await mermaid.render(`relay-diagram-${++drawn}`, source);
  return svg;
}
