# Layout acceptance

Three distinct claims must remain separate:

1. **Geometry passed:** measured node/text boxes and orthogonal arrow centerlines passed the bundled checker.
2. **Preview inspected:** an agent actually opened the generated PNG/SVG and checked readability and meaning.
3. **Native editor inspected:** an agent actually imported/opened the scene in Excalidraw and verified the resulting editor rendering.

The CLI sets both inspection fields to `pending`. Record the actual tool, artifact and findings in an adjacent review JSON/Markdown; do not infer a visual pass from exit status.

## Automatic checks

- Node overlap/containment, group boundaries, group title overflow and measured label overflow.
- Label-to-label and label-to-node intersections, with clearance.
- Every routed arrow segment against unrelated node boxes and every label box, including its own edge label.
- Arrow crossings and coincident segments. Separate ports prevent fan-out from silently sharing the same segment.
- Explicit group ancestry permits intended containment, rather than exempting all contained objects.
- Output counts, unique IDs and reciprocal text/arrow bindings preserve source content.

Node separation uses a 12 px margin; arrow-to-obstacle checks use 5 px. Exact details live in `scripts/geometry.mjs`. These are conservative geometric checks, not pixel-perfect proof: arrowheads, antialiasing, rounded borders, editor font fallback and post-import transformations still require visual review. Group border crossings by relationships are intentional; group-title collisions are not.

## Review and repair

Inspect both the whole diagram and labels/endpoints at readable zoom. Check Chinese glyphs, line wraps, branch labels, return paths, group headers and whitespace. Use full-resolution preview details if a large thumbnail is too small. Keep text around 18–26 px in the native canvas; split a view instead of shrinking everything.

The engine retries spacing three times. An unresolved crossing is a failure, even if all nodes are separate. Adjust meaningful grouping, choose another direction when the source does not constrain it, or split overview/detail/scenario views. Track all source edge IDs across the split. A non-planar graph (for example K3,3) cannot be drawn in a single plane without a crossing, so universal single-view guarantees are false.

An overview may summarize relationships only if detail views preserve the original relationships and the overview is labeled as a summary. Never create routing nodes that look like actual system components. Repeated boundary nodes must retain the same source identity and be visibly identified as repeated context.

## Editing and portability

The native file contains editable shapes, bound node text, bound arrows and grouped edge labels; no rasterized diagram is embedded. Explicit polyline points use `elbowed: false` to preserve routed coordinates. Edge labels stay in an arrow group, not a midpoint-bound text container, because a midpoint label can cover an unrelated node. Moving endpoints later requires rechecking labels.

Font metrics are local. A machine with different fonts may render labels differently. PNG preserves the inspected appearance; `.excalidraw` preserves editability. If the native editor reflows labels, inspect and adjust before claiming native compatibility for that artifact.

## Maintenance tests

Run `npm test`. Coverage includes schema validation, Mermaid semantics and rejection behavior, multiple Markdown blocks, CJK measurement, all four directions, nested groups/cycles/self-loops/fan-out, obstacle collisions, arrow crossings/shared segments, native bindings, PNG output, overwrite protection and a deliberately failing non-planar graph that retains every relationship.
