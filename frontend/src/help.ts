/** The guides shown under Help. Their text is loaded with the Help page, not with the app. */
export const HELP_DOCS = [
  { id: "user-manual", title: "User manual", file: "user-manual.md" },
  { id: "algorithms", title: "Algorithm guide", file: "algorithms.md" },
  { id: "deployment", title: "Installation and settings", file: "deployment.md" },
  { id: "ai-models", title: "AI models", file: "ai-models.md" },
  { id: "validation", title: "Detector validation", file: "validation/README.md" },
  { id: "overview", title: "Technical overview", file: "README.md" },
] as const;

export type HelpDocId = (typeof HELP_DOCS)[number]["id"];

/** `#/help/<doc>[/<heading id>]`, or null when the address is not a Help page. */
export function readHelpHash(): { doc: HelpDocId; anchor: string } | null {
  const m = location.hash.match(/^#\/help(?:\/([\w-]+))?(?:\/(.+))?/);
  if (!m) return null;
  const doc = HELP_DOCS.find((d) => d.id === m[1])?.id ?? "user-manual";
  return { doc, anchor: m[2] ? decodeURIComponent(m[2]) : "" };
}
