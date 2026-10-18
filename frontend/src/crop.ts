import type { ChainNode, FilterSchema, Graph } from "./types";

/** Filters that keep every pixel where it was: a crop can run before them and the result covers the same area. */
const PIXEL_ALIGNED = new Set(["Deblur", "Denoise", "Sharpen", "Exposure", "Colour"]);

const spec = (filters: FilterSchema[], n: ChainNode) => filters.find((f) => f.id === n.filter);

/** True when a crop may be moved in front of this step without changing what the step does. Steps that hold
 * image coordinates (a motion path drawn on the image, a region) stay in front of the crop. */
function cropCanPrecede(filters: FilterSchema[], n: ChainNode): boolean {
  if (!n.enabled) return true;
  const f = spec(filters, n);
  if (!f || !PIXEL_ALIGNED.has(f.category)) return false;
  if (f.id === "exposure.curves") return true; // its points are curve handles, not image positions
  return !f.params.some((p) => p.type === "points" && Array.isArray(n.params[p.name]) && (n.params[p.name] as unknown[]).length > 0);
}

/** Where a crop drawn on the output of step `at - 1` should go so that every enhancement runs on the cropped
 * area only: as early as possible, but never in front of a step that moves pixels (rotate, perspective,
 * stabilise, resize...), because the box was drawn in that step's output coordinates. */
export function cropPosition(filters: FilterSchema[], nodes: ChainNode[], at: number): number {
  let i = Math.min(Math.max(at, 0), nodes.length);
  while (i > 0 && nodes[i - 1].filter !== "geometry.crop" && cropCanPrecede(filters, nodes[i - 1])) i--;
  return i;
}

/** Index of the last enabled crop in the chain, or -1. */
export function lastCropIndex(graph: Graph | null): number {
  if (!graph) return -1;
  for (let i = graph.nodes.length - 1; i >= 0; i--) {
    if (graph.nodes[i].enabled && graph.nodes[i].filter === "geometry.crop") return i;
  }
  return -1;
}

/** The chain's framing without its enhancements: the steps up to the last crop, minus the pixel-only filters.
 * This is "the original" for a cropped chain: the same area of the source, untouched, so a swipe or flip
 * compares like with like. Null when the chain has no crop. */
export function framingGraph(filters: FilterSchema[], graph: Graph | null): Graph | null {
  const last = lastCropIndex(graph);
  if (!graph || last < 0) return null;
  const nodes = graph.nodes.slice(0, last + 1).filter((n) => {
    if (!n.enabled) return false;
    const f = spec(filters, n);
    return !f || !PIXEL_ALIGNED.has(f.category);
  });
  return { ...graph, nodes };
}

/** The crop box on the full source, when it can be drawn there (only pixel-only steps before the first crop). */
export function cropBoxOnSource(filters: FilterSchema[], graph: Graph | null): number[][] | null {
  if (!graph) return null;
  const first = graph.nodes.findIndex((n) => n.enabled && n.filter === "geometry.crop");
  if (first < 0) return null;
  if (!graph.nodes.slice(0, first).every((n) => cropCanPrecede(filters, n))) return null;
  const p = graph.nodes[first].params as { x?: number; y?: number; width?: number; height?: number };
  if (p.x == null || p.y == null || !p.width || !p.height) return null;
  return [
    [p.x, p.y],
    [p.x + p.width - 1, p.y + p.height - 1],
  ];
}
