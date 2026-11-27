import ELK from 'elkjs/lib/elk.bundled.js';
import { createCanvas, GlobalFonts } from '@napi-rs/canvas';
import { checkLayout, segmentHitsRect, segments } from './geometry.mjs';

if (process.env.EXCALIDRAW_FONT_PATH && !GlobalFonts.registerFromPath(process.env.EXCALIDRAW_FONT_PATH, 'DiagramCJK')) {
  throw new Error('Could not load EXCALIDRAW_FONT_PATH');
}
const families = new Set(GlobalFonts.families.map(f => f.family));
const cjk = ['DiagramCJK', 'PingFang SC', 'Hiragino Sans GB', 'Noto Sans CJK SC', 'Noto Sans SC', 'Microsoft YaHei', 'SimHei', 'Heiti SC', 'Heiti TC', 'WenQuanYi Zen Hei'].find(f => families.has(f));
export const FONT = `Arial, Helvetica, ${cjk ? `"${cjk}", ` : ''}sans-serif`;
const canvas = createCanvas(10, 10), ctx = canvas.getContext('2d');
export function measure(text, size = 20, maxWidth = 240) {
  if (/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]/u.test(text) && !cjk) {
    throw new Error('No CJK font is available. Set EXCALIDRAW_FONT_PATH to a local CJK .ttf/.otf/.ttc file; do not export missing-glyph boxes.');
  }
  ctx.font = `${size}px ${FONT}`;
  const lines = [];
  for (const paragraph of text.split('\n')) {
    let line = '';
    for (const char of paragraph) {
      if (line && ctx.measureText(line + char).width > maxWidth) { lines.push(line); line = ''; }
      line += char;
    }
    lines.push(line);
  }
  return { lines, text: lines.join('\n'), fontSize: size, lineHeight: size * 1.4,
    width: Math.ceil(Math.max(1, ...lines.map(l => ctx.measureText(l).width))) + 6,
    height: Math.ceil(lines.length * size * 1.4) };
}

function projectEndpoint(node, point, neighbor, gap = 6) {
  const cx = node.x + node.width / 2, cy = node.y + node.height / 2;
  const a = node.width / 2, b = node.height / 2;
  const horizontal = Math.abs(point.y - neighbor.y) < 0.05;
  if (horizontal) {
    const t = Math.min(0.98, Math.abs((point.y - cy) / b));
    const dx = node.shape === 'ellipse' ? a * Math.sqrt(1 - t * t) : node.shape === 'diamond' ? a * (1 - t) : a;
    return { x: cx + (neighbor.x >= cx ? 1 : -1) * (dx + gap), y: point.y };
  }
  const t = Math.min(0.98, Math.abs((point.x - cx) / a));
  const dy = node.shape === 'ellipse' ? b * Math.sqrt(1 - t * t) : node.shape === 'diamond' ? b * (1 - t) : b;
  return { x: point.x, y: cy + (neighbor.y >= cy ? 1 : -1) * (dy + gap) };
}

export async function route(model, attempt = 0) {
  const scale = [1, 1.5, 2.2][attempt] ?? 2.2;
  const direction = { LR: 'RIGHT', RL: 'LEFT', TB: 'DOWN', BT: 'UP' }[model.direction];
  const sides = { LR: ['EAST', 'WEST'], RL: ['WEST', 'EAST'], TB: ['SOUTH', 'NORTH'], BT: ['NORTH', 'SOUTH'] }[model.direction];
  const horizontal = ['LR', 'RL'].includes(model.direction);
  const ports = new Map(model.nodes.map(n => [n.id, []]));
  // Dedicated ports stop fan-out edges from sharing their first/last segment.
  for (const [i, e] of model.edges.entries()) {
    ports.get(e.from).push({ id: `port_${i}_from`, width: 0, height: 0, layoutOptions: { 'elk.port.side': sides[0] } });
    ports.get(e.to).push({ id: `port_${i}_to`, width: 0, height: 0, layoutOptions: { 'elk.port.side': sides[1] } });
  }
  const nodeMeta = new Map(), groupMeta = new Map(), edgeMeta = new Map();
  const elkNodes = new Map();
  for (const n of model.nodes) {
    const metrics = measure(n.label);
    const factor = n.shape === 'diamond' ? 2.1 : n.shape === 'ellipse' ? 1.55 : 1;
    const degree = Math.max(...sides.map(side => ports.get(n.id).filter(p => p.layoutOptions['elk.port.side'] === side).length), 1);
    const width = Math.ceil(Math.max(180, metrics.width * factor + 44, horizontal ? 0 : degree * 26 + 60));
    const height = Math.ceil(Math.max(80, metrics.height * factor + 40, horizontal ? degree * 26 + 52 : 0));
    nodeMeta.set(n.id, { ...n, metrics, width, height });
    elkNodes.set(n.id, { id: n.id, width, height, ports: ports.get(n.id),
      layoutOptions: { 'elk.portConstraints': 'FIXED_SIDE', 'elk.spacing.portPort': '24' } });
  }
  for (const g of model.groups) {
    const metrics = measure(g.label, 18, 160);
    groupMeta.set(g.id, { ...g, metrics });
    elkNodes.set(g.id, { id: g.id, children: [], layoutOptions: {
      'elk.padding': `[top=${metrics.height + 40},left=32,bottom=32,right=32]`,
      'elk.spacing.nodeNode': String(64 * scale),
      'elk.layered.spacing.nodeNodeBetweenLayers': String(120 * scale),
      'elk.spacing.edgeLabel': '14' } });
  }
  const root = { id: '__root__', children: [], edges: [], layoutOptions: {
    'elk.algorithm': 'layered', 'elk.direction': direction, 'elk.edgeRouting': 'ORTHOGONAL',
    'elk.hierarchyHandling': 'INCLUDE_CHILDREN', 'elk.padding': '[top=20,left=20,bottom=20,right=20]',
    'elk.spacing.nodeNode': String(64 * scale), 'elk.spacing.edgeEdge': String(24 * scale),
    'elk.spacing.edgeNode': String(28 * scale), 'elk.spacing.edgeLabel': '14',
    'elk.layered.spacing.nodeNodeBetweenLayers': String(120 * scale),
    'elk.layered.spacing.edgeEdgeBetweenLayers': String(24 * scale),
    'elk.layered.spacing.edgeNodeBetweenLayers': String(28 * scale),
    'elk.layered.mergeEdges': 'false', 'elk.randomSeed': String(71 + attempt * 17) } };
  for (const item of [...model.groups, ...model.nodes]) {
    const parent = item.group ? elkNodes.get(item.group) : root;
    parent.children.push(elkNodes.get(item.id));
  }
  for (const [i, e] of model.edges.entries()) {
    const metrics = e.label ? measure(e.label, 18, 180) : null;
    edgeMeta.set(e.id, { ...e, metrics });
    root.edges.push({ id: e.id, sources: [`port_${i}_from`], targets: [`port_${i}_to`],
      ...(metrics ? { labels: [{ text: metrics.text, width: metrics.width + 8, height: metrics.height + 8,
        layoutOptions: { 'elk.edgeLabels.placement': 'CENTER', 'elk.edgeLabels.inline': 'false' } }] } : {}) });
  }
  const computed = await new ELK().layout(root);
  const titleMetrics = model.title ? measure(model.title, 26, Math.max(280, Math.min(computed.width, 900))) : null;
  const origin = { x: 40, y: titleMetrics ? titleMetrics.height + 70 : 40 };
  const origins = new Map([['__root__', origin]]), nodes = [], groups = [];
  const walk = (parent, offset) => {
    for (const item of parent.children ?? []) {
      const x = offset.x + item.x, y = offset.y + item.y;
      origins.set(item.id, { x, y });
      if (groupMeta.has(item.id)) {
        const g = groupMeta.get(item.id);
        groups.push({ ...g, x, y, width: item.width, height: item.height,
          textBox: { x: x + 20, y: y + 16, width: g.metrics.width, height: g.metrics.height } });
        walk(item, { x, y });
      } else {
        const n = nodeMeta.get(item.id);
        nodes.push({ ...n, x, y, textBox: { x: x + (n.width - n.metrics.width) / 2,
          y: y + (n.height - n.metrics.height) / 2, width: n.metrics.width, height: n.metrics.height } });
      }
    }
  };
  walk(computed, origin);
  const byId = new Map(nodes.map(n => [n.id, n]));
  const edges = computed.edges.map(e => {
    if (e.sections?.length !== 1) throw new Error(`${e.id}: expected exactly one routed section, cannot safely export`);
    const offset = origins.get(e.container ?? '__root__');
    if (!offset) throw new Error(`${e.id}: unknown coordinate container`);
    const section = e.sections[0], meta = edgeMeta.get(e.id);
    const points = [section.startPoint, ...(section.bendPoints ?? []), section.endPoint]
      .map(p => ({ x: p.x + offset.x, y: p.y + offset.y }));
    points[0] = projectEndpoint(byId.get(meta.from), points[0], points[1]);
    points[points.length - 1] = projectEndpoint(byId.get(meta.to), points.at(-1), points.at(-2));
    const lb = e.labels?.[0];
    return { ...meta, points, ...(lb ? { textBox: { x: offset.x + lb.x, y: offset.y + lb.y, width: lb.width, height: lb.height } } : {}) };
  });
  // Incoming edges can cross the header band. Move the header within that
  // reserved band instead of covering an arrow or deleting its relationship.
  for (const group of groups) {
    const left = group.x + 20, right = group.x + group.width - group.textBox.width - 20;
    const candidates = [left, (left + right) / 2, right];
    for (let x = left; x <= right; x += 12) candidates.push(x);
    for (const x of candidates) {
      const box = { ...group.textBox, x };
      if (!edges.some(edge => segments(edge.points).some(([a, b]) => segmentHitsRect(a, b, box, 8)))) {
        group.textBox = box; break;
      }
    }
  }
  const title = titleMetrics ? { x: 60, y: 24, width: titleMetrics.width, height: titleMetrics.height, metrics: titleMetrics } : null;
  return { nodes, groups, edges, title, width: Math.max(computed.width + 80, (title?.width ?? 0) + 120),
    height: computed.height + origin.y + 40, attempt, direction: model.direction };
}

export async function buildLayout(model) {
  let best;
  const attempts = [];
  for (let i = 0; i < 3; i++) {
    const layout = await route(model, i), qa = checkLayout(layout);
    attempts.push({ attempt: i + 1, errors: qa.errors.length });
    if (!best || qa.errors.length < best.qa.errors.length) best = { layout, qa };
    if (qa.pass) break;
  }
  best.qa.attempts = attempts;
  best.qa.warnings = model.warnings;
  return best;
}
