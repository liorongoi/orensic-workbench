import { useEffect, useMemo, useRef, useState } from "react";
import { Marked } from "marked";
// The guides are bundled into the app at build time, so Help works offline and always
// matches the installed version.
import manual from "../../../docs/user-manual.md?raw";
import algorithms from "../../../docs/algorithms.md?raw";
import ampedComparison from "../../../docs/amped-comparison.md?raw";
import deployment from "../../../docs/deployment.md?raw";
import aiModels from "../../../docs/ai-models.md?raw";
import validation from "../../../docs/validation/README.md?raw";
import trafficDoc from "../../../docs/traffic.md?raw";
import overview from "../../../README.md?raw";
import { HELP_DOCS, type HelpDocId, readHelpHash } from "../help";

const TEXT: Record<HelpDocId, string> = {
  "user-manual": manual,
  algorithms,
  "amped-comparison": ampedComparison,
  deployment,
  "ai-models": aiModels,
  validation,
  traffic: trafficDoc,
  overview,
};

type Heading = { id: string; text: string; depth: number };

/** GitHub's heading anchors, so the guides' own links ("#7-enhance") work here too. */
function slug(text: string, seen: Map<string, number>) {
  const base = text
    .toLowerCase()
    .trim()
    .replace(/[^\p{L}\p{N}\s_-]/gu, "")
    .replace(/\s/g, "-");
  const n = seen.get(base) ?? 0;
  seen.set(base, n + 1);
  return n ? `${base}-${n}` : base;
}

const plain = (html: string) => {
  const el = document.createElement("div");
  el.innerHTML = html;
  return el.textContent ?? "";
};

function render(text: string) {
  const headings: Heading[] = [];
  const seen = new Map<string, number>();
  const md = new Marked({ gfm: true });
  md.use({
    renderer: {
      heading({ tokens, depth }) {
        const inner = this.parser.parseInline(tokens);
        const label = plain(inner);
        const id = slug(label, seen);
        headings.push({ id, text: label, depth });
        return `<h${depth} id="${id}">${inner}</h${depth}>\n`;
      },
    },
  });
  return { html: md.parse(text) as string, headings };
}

/** "#7-enhance", "algorithms.md#3-decoding" or "../README.md" -> where it points in Help, if anywhere. */
function resolve(href: string, current: HelpDocId): { doc: HelpDocId; anchor: string } | null {
  if (href.startsWith("#")) return { doc: current, anchor: decodeURIComponent(href.slice(1)) };
  const [path, anchor = ""] = href.split("#");
  const top = /^(\.\.\/)+README\.md$/.test(path); // the repository README, linked from docs/
  const name = top ? "README.md" : path.replace(/^(\.\.?\/)+/, "").replace(/^docs\//, "");
  const doc = HELP_DOCS.find((d) => d.file === name);
  return doc ? { doc: doc.id, anchor: decodeURIComponent(anchor) } : null;
}

export default function HelpPage() {
  const [route, setRoute] = useState(() => readHelpHash() ?? { doc: "user-manual" as HelpDocId, anchor: "" });
  const [filter, setFilter] = useState("");
  const body = useRef<HTMLDivElement>(null);
  const doc = HELP_DOCS.find((d) => d.id === route.doc) ?? HELP_DOCS[0];
  const { html, headings } = useMemo(() => render(TEXT[doc.id]), [doc]);

  useEffect(() => {
    const onHash = () => {
      const r = readHelpHash();
      if (r) setRoute(r);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    document.title = `${doc.title} · Lucida`;
    const el = route.anchor ? document.getElementById(route.anchor) : null;
    if (el) el.scrollIntoView();
    else body.current?.scrollTo(0, 0);
  }, [doc, route.anchor, html]);

  const go = (docId: HelpDocId, anchor = "") => {
    location.hash = `#/help/${docId}${anchor ? `/${encodeURIComponent(anchor)}` : ""}`;
  };

  // Links inside a guide: keep them in Help instead of letting them change the app's route.
  const onClick = (e: React.MouseEvent) => {
    const a = (e.target as HTMLElement).closest("a");
    const href = a?.getAttribute("href");
    if (!a || !href || /^[a-z]+:/i.test(href)) return;
    e.preventDefault();
    const to = resolve(href, doc.id);
    if (to) go(to.doc, to.anchor);
  };

  const q = filter.trim().toLowerCase();
  const toc = headings.filter((h) => h.depth >= 2 && h.depth <= 3 && (!q || h.text.toLowerCase().includes(q)));

  return (
    <div className="help">
      <nav className="help-nav">
        <div className="help-docs">
          {HELP_DOCS.map((d) => (
            <button key={d.id} className={d.id === doc.id ? "active" : ""} onClick={() => go(d.id)}>
              {d.title}
            </button>
          ))}
        </div>
        <input type="text" placeholder="Find a section" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <ul className="help-toc">
          {toc.map((h) => (
            <li key={h.id} className={`d${h.depth}`}>
              <a href={`#/help/${doc.id}/${encodeURIComponent(h.id)}`}>{h.text}</a>
            </li>
          ))}
          {toc.length === 0 && <li className="muted small">No section matches.</li>}
        </ul>
        <p className="muted small">Your browser's find (Ctrl+F or ⌘F) searches the whole text.</p>
      </nav>
      <div className="help-body" ref={body}>
        <article className="help-doc" onClick={onClick} dangerouslySetInnerHTML={{ __html: html }} />
      </div>
    </div>
  );
}
