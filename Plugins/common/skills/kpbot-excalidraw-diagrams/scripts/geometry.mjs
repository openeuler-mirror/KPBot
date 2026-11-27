// Geometry checks operate on routed coordinates, not on a renderer screenshot.
const EPS = 0.05;
export const rect = (x, y, width, height, id) => ({ x, y, width, height, id });
export function overlaps(a, b, margin = 0) {
  return Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x) > -margin + EPS &&
    Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y) > -margin + EPS;
}
export function contains(outer, inner, padding = 0) {
  return inner.x >= outer.x + padding - EPS && inner.y >= outer.y + padding - EPS &&
    inner.x + inner.width <= outer.x + outer.width - padding + EPS &&
    inner.y + inner.height <= outer.y + outer.height - padding + EPS;
}
export function segmentHitsRect(a, b, box, padding = 0) {
  // Open interior: touching a box edge is not cutting through it.
  const x1 = box.x - padding + EPS, x2 = box.x + box.width + padding - EPS;
  const y1 = box.y - padding + EPS, y2 = box.y + box.height + padding - EPS;
  if (Math.abs(a.x - b.x) < EPS) {
    return a.x > x1 && a.x < x2 && Math.max(a.y, b.y) > y1 && Math.min(a.y, b.y) < y2;
  }
  if (Math.abs(a.y - b.y) < EPS) {
    return a.y > y1 && a.y < y2 && Math.max(a.x, b.x) > x1 && Math.min(a.x, b.x) < x2;
  }
  throw new Error('A routed segment is not orthogonal');
}
export function segmentIntersection(a, b, c, d) {
  const ah = Math.abs(a.y - b.y) < EPS, ch = Math.abs(c.y - d.y) < EPS;
  if (ah === ch) {
    const lineA = ah ? a.y : a.x, lineC = ah ? c.y : c.x;
    if (Math.abs(lineA - lineC) >= EPS) return null;
    const aa = ah ? [a.x, b.x] : [a.y, b.y], cc = ah ? [c.x, d.x] : [c.y, d.y];
    const length = Math.min(Math.max(...aa), Math.max(...cc)) - Math.max(Math.min(...aa), Math.min(...cc));
    return length > EPS ? { type: 'shared_segment', length } : null;
  }
  const h1 = ah ? a : c, h2 = ah ? b : d, v1 = ah ? c : a, v2 = ah ? d : b;
  if (v1.x >= Math.min(h1.x, h2.x) - EPS && v1.x <= Math.max(h1.x, h2.x) + EPS &&
      h1.y >= Math.min(v1.y, v2.y) - EPS && h1.y <= Math.max(v1.y, v2.y) + EPS) {
    return { type: 'crossing', x: v1.x, y: h1.y };
  }
  return null;
}
export const segments = points => points.slice(1).map((p, i) => [points[i], p]);

export function checkLayout(layout) {
  const errors = [];
  const add = (kind, ids, detail) => errors.push({ kind, ids, ...(detail ? { detail } : {}) });
  const { nodes, groups, edges, title } = layout;
  const nodeMap = new Map(nodes.map(n => [n.id, n]));
  const groupMap = new Map(groups.map(g => [g.id, g]));
  const ancestor = (parent, child) => {
    let id = child.group;
    while (id) { if (id === parent.id) return true; id = groupMap.get(id)?.group; }
    return false;
  };
  for (let i = 0; i < nodes.length; i++) {
    const a = nodes[i];
    if (!contains(a, a.textBox, 12)) add('text_overflow', [a.id]);
    for (const b of nodes.slice(i + 1)) if (overlaps(a, b, 12)) add('node_overlap_or_gap', [a.id, b.id]);
    for (const g of groups) {
      if (ancestor(g, a)) {
        if (!contains(g, a, 12)) add('group_overflow', [g.id, a.id]);
      } else if (overlaps(g, a)) add('unrelated_group_overlap', [g.id, a.id]);
    }
  }
  for (let i = 0; i < groups.length; i++) for (const b of groups.slice(i + 1)) {
    const a = groups[i];
    if (ancestor(a, b) || ancestor(b, a)) continue;
    if (overlaps(a, b, 12)) add('group_overlap_or_gap', [a.id, b.id]);
  }
  for (const group of groups) if (!contains(group, group.textBox, 10)) add('group_label_overflow', [group.id]);
  const labels = [...nodes.map(n => ({ ...n.textBox, owner: n.id, role: 'node' })),
    ...groups.map(g => ({ ...g.textBox, owner: g.id, role: 'group' })),
    ...edges.filter(e => e.textBox).map(e => ({ ...e.textBox, owner: e.id, role: 'edge' })),
    ...(title ? [{ ...title, owner: 'title', role: 'title' }] : [])];
  for (let i = 0; i < labels.length; i++) {
    const a = labels[i];
    for (const b of labels.slice(i + 1)) if (overlaps(a, b, 5)) add('label_overlap', [a.owner, b.owner]);
    if (a.role !== 'node') for (const n of nodes) if (overlaps(a, n, 5)) add('label_node_overlap', [a.owner, n.id]);
  }
  for (const e of edges) {
    if (!nodeMap.has(e.from) || !nodeMap.has(e.to)) add('missing_endpoint', [e.id]);
    for (const [index, [a, b]] of segments(e.points).entries()) {
      if (Math.abs(a.x - b.x) > EPS && Math.abs(a.y - b.y) > EPS) {
        add('non_orthogonal_segment', [e.id]); continue;
      }
      for (const n of nodes) {
        // Endpoints may touch their own shape, but no segment may traverse its interior.
        // Nonrectangular endpoint shapes use the logical box only for unrelated edges.
        if (n.shape !== 'rectangle' && ((n.id === e.from && index === 0) || (n.id === e.to && index === e.points.length - 2))) continue;
        if (segmentHitsRect(a, b, n, n.id === e.from || n.id === e.to ? -1 : 5)) add('edge_through_node', [e.id, n.id]);
      }
      for (const label of labels) if (segmentHitsRect(a, b, label, 4)) add('edge_through_label', [e.id, label.owner]);
    }
  }
  for (let i = 0; i < edges.length; i++) for (const b of edges.slice(i + 1)) {
    const a = edges[i];
    for (const [a1, a2] of segments(a.points)) for (const [b1, b2] of segments(b.points)) {
      const hit = segmentIntersection(a1, a2, b1, b2);
      if (hit) add(hit.type === 'crossing' ? 'edge_crossing' : 'edge_shared_segment', [a.id, b.id], hit);
    }
  }
  const seen = new Set();
  const unique = errors.filter(e => { const key = `${e.kind}:${e.ids.join(':')}`; if (seen.has(key)) return false; seen.add(key); return true; });
  return { pass: unique.length === 0, errors: unique, counts: { nodes: nodes.length, edges: edges.length, groups: groups.length },
    scope: 'Logical geometry with 5px edge clearance, 12px node gap and measured labels; includes edge crossings/shared segments. Group membership and bound text are intentional containment.',
    visualInspection: 'pending', nativeEditorInspection: 'pending' };
}
