import { useEffect, useState, useSyncExternalStore } from "react";
import { useIsCodeFenceIncomplete } from "streamdown";

/** The page's theme, followed as it changes (lib/theme.ts sets <html data-theme>), so a diagram is drawn again in
 *  the new colours. */
function onThemeChange(change: () => void) {
  const observer = new MutationObserver(change);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  return () => observer.disconnect();
}
const currentTheme = () => document.documentElement.dataset.theme ?? "light";

/** A code fence tagged mermaid in rich text (components/Rich.tsx), drawn as a diagram. Mermaid loads the first time
 *  a diagram is shown. While the fence is still being written, or when Mermaid cannot read it, its source shows as
 *  code instead. */
export default function Diagram({ source }: { source: string }) {
  const writing = useIsCodeFenceIncomplete();
  const theme = useSyncExternalStore(onThemeChange, currentTheme);
  const [svg, setSvg] = useState("");
  useEffect(() => {
    if (writing) return;
    let live = true;
    import("../lib/richDiagrams")
      .then(({ renderDiagram }) => renderDiagram(source))
      .then(
        (drawn) => live && setSvg(drawn),
        () => live && setSvg(""),
      );
    return () => {
      live = false;
    };
  }, [source, writing, theme]);
  if (!svg)
    return (
      <pre>
        <code className="language-mermaid">{source}</code>
      </pre>
    );
  // Mermaid's own output, drawn in strict mode: labels are text, sanitised by Mermaid, with no scripts or handlers.
  return <div className="diagram" role="img" aria-label="Diagram" dangerouslySetInnerHTML={{ __html: svg }} />;
}
