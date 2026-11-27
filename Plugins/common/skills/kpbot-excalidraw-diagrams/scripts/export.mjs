import { createHash } from 'node:crypto';
import { createCanvas } from '@napi-rs/canvas';
import { FONT } from './layout.mjs';

const sid = (role, id) => `${role}_${createHash('sha256').update(id).digest('hex').slice(0, 18)}`;
const round = value => Math.round(value * 100) / 100;
const palette = node => node.shape === 'diamond' ? ['#a16207', '#fff4cf'] : node.role === 'store' ? ['#2563a5', '#e8f2ff'] : ['#176c64', '#eaf6f2'];
function base(type, id, box, extras = {}) {
  return { id, type, x: round(box.x), y: round(box.y), width: round(box.width), height: round(box.height),
    angle: 0, strokeColor: '#263849', backgroundColor: 'transparent', fillStyle: 'solid', strokeWidth: 2,
    strokeStyle: 'solid', roughness: 0, opacity: 100, groupIds: [], frameId: null, roundness: null,
    seed: parseInt(createHash('sha256').update(id).digest('hex').slice(0, 7), 16), version: 1, versionNonce: 1,
    isDeleted: false, boundElements: [], updated: 1, link: null, locked: false, ...extras };
}

export function toScene(layout, model) {
  const result = [], byNode = new Map(layout.nodes.map(n => [n.id, n]));
  const groupMap = new Map(model.groups.map(g => [g.id, g]));
  const memberships = item => {
    const names = []; let parent = item.group;
    while (parent) { names.push(sid('group', parent)); parent = groupMap.get(parent)?.group; }
    return names;
  };
  const text = (id, content, metrics, box, extras = {}) => base('text', id, box, {
    text: metrics.text, originalText: content, fontSize: metrics.fontSize, fontFamily: 2,
    textAlign: 'center', verticalAlign: 'middle', containerId: null, autoResize: false,
    lineHeight: 1.4, strokeWidth: 1, ...extras });
  for (const g of layout.groups) {
    const groupIds = [sid('group', g.id), ...memberships(g)];
    result.push(base('rectangle', sid('zone', g.id), g, { groupIds, backgroundColor: '#f8fafc',
      strokeColor: '#8fa5b8', strokeStyle: 'dashed', roundness: { type: 3 }, customData: { role: 'group', sourceId: g.id } }));
    result.push(text(sid('zone-label', g.id), g.label, g.metrics, g.textBox, { groupIds, textAlign: 'left', customData: { role: 'group-label', sourceId: g.id } }));
  }
  const binding = (id, point, neighbor) => {
    const n = byNode.get(id), horizontal = Math.abs(point.y - neighbor.y) < 0.1;
    const focus = horizontal ? (point.y - n.y - n.height / 2) / (n.height / 2) : (point.x - n.x - n.width / 2) / (n.width / 2);
    return { elementId: sid('node', id), focus: Math.max(-1, Math.min(1, round(focus))), gap: 6 };
  };
  for (const e of layout.edges) {
    const start = e.points[0], end = e.points.at(-1), xs = e.points.map(p => p.x), ys = e.points.map(p => p.y);
    result.push(base('arrow', sid('edge', e.id), { ...start, width: Math.max(...xs) - Math.min(...xs), height: Math.max(...ys) - Math.min(...ys) }, {
      points: e.points.map(p => [round(p.x - start.x), round(p.y - start.y)]),
      startBinding: binding(e.from, start, e.points[1]), endBinding: binding(e.to, end, e.points.at(-2)),
      startArrowhead: e.heads === 'both' ? 'arrow' : null, endArrowhead: e.heads === 'none' ? null : 'arrow',
      elbowed: false, lastCommittedPoint: null, strokeWidth: e.style === 'thick' ? 3.5 : 2,
      strokeStyle: e.style === 'dashed' ? 'dashed' : 'solid', groupIds: [sid('connection', e.id)],
      customData: { role: 'edge', sourceId: e.id, from: e.from, to: e.to } }));
  }
  for (const n of layout.nodes) {
    const [strokeColor, backgroundColor] = palette(n), groupIds = memberships(n);
    result.push(base(n.shape, sid('node', n.id), n, { strokeColor, backgroundColor, groupIds,
      roundness: n.shape === 'rectangle' ? { type: 3 } : null,
      boundElements: [{ id: sid('text', n.id), type: 'text' }, ...layout.edges.filter(e => e.from === n.id || e.to === n.id).map(e => ({ id: sid('edge', e.id), type: 'arrow' }))],
      customData: { role: 'node', sourceId: n.id, sourceShape: n.sourceShape ?? n.shape } }));
    result.push(text(sid('text', n.id), n.label, n.metrics, n.textBox, { containerId: sid('node', n.id), groupIds,
      customData: { role: 'node-label', sourceId: n.id } }));
  }
  for (const e of layout.edges.filter(e => e.textBox)) {
    result.push(text(sid('edge-label', e.id), e.label, e.metrics, {
      x: e.textBox.x + 4, y: e.textBox.y + 4, width: e.textBox.width - 8, height: e.textBox.height - 8 }, {
      groupIds: [sid('connection', e.id)], customData: { role: 'edge-label', sourceId: e.id } }));
  }
  if (layout.title) result.push(text('diagram_title', model.title, layout.title.metrics, layout.title, { textAlign: 'left', customData: { role: 'title' } }));
  const ids = new Set(result.map(e => e.id));
  if (ids.size !== result.length) throw new Error('Export ID collision');
  for (const element of result) for (const bound of element.boundElements) if (!ids.has(bound.id)) throw new Error('Export contains an invalid binding');
  return { type: 'excalidraw', version: 2, source: 'local-excalidraw-diagrams', elements: result,
    appState: { viewBackgroundColor: '#ffffff', gridSize: null, currentItemFontFamily: 2 }, files: {} };
}

const xml = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
export function toSvg(layout) {
  let s = `<svg xmlns="http://www.w3.org/2000/svg" width="${Math.ceil(layout.width)}" height="${Math.ceil(layout.height)}" viewBox="0 0 ${layout.width} ${layout.height}"><rect width="100%" height="100%" fill="white"/>`;
  const txt = (metrics, box, align = 'center') => metrics.lines.map((line, i) => `<text x="${align === 'left' ? box.x : box.x + box.width / 2}" y="${box.y + metrics.fontSize + i * metrics.lineHeight}" text-anchor="${align === 'left' ? 'start' : 'middle'}" font-family="${xml(FONT)}" font-size="${metrics.fontSize}" fill="#263849">${xml(line)}</text>`).join('');
  for (const g of layout.groups) s += `<rect x="${g.x}" y="${g.y}" width="${g.width}" height="${g.height}" rx="8" fill="#f8fafc" stroke="#8fa5b8" stroke-width="2" stroke-dasharray="8 6"/>` + txt(g.metrics, g.textBox, 'left');
  const tip = (p, q) => {
    const angle = Math.atan2(p.y - q.y, p.x - q.x), size = 11;
    return `<path d="M ${p.x - size * Math.cos(angle - 0.5)} ${p.y - size * Math.sin(angle - 0.5)} L ${p.x} ${p.y} L ${p.x - size * Math.cos(angle + 0.5)} ${p.y - size * Math.sin(angle + 0.5)}" fill="none" stroke="#263849" stroke-width="2"/>`;
  };
  for (const e of layout.edges) {
    s += `<polyline points="${e.points.map(p => `${p.x},${p.y}`).join(' ')}" fill="none" stroke="#263849" stroke-width="${e.style === 'thick' ? 3.5 : 2}" ${e.style === 'dashed' ? 'stroke-dasharray="8 6"' : ''}/>`;
    if (e.heads !== 'none') s += tip(e.points.at(-1), e.points.at(-2));
    if (e.heads === 'both') s += tip(e.points[0], e.points[1]);
  }
  for (const n of layout.nodes) {
    const [stroke, fill] = palette(n), style = `fill="${fill}" stroke="${stroke}" stroke-width="2"`;
    if (n.shape === 'ellipse') s += `<ellipse cx="${n.x + n.width / 2}" cy="${n.y + n.height / 2}" rx="${n.width / 2}" ry="${n.height / 2}" ${style}/>`;
    else if (n.shape === 'diamond') s += `<polygon points="${n.x + n.width / 2},${n.y} ${n.x + n.width},${n.y + n.height / 2} ${n.x + n.width / 2},${n.y + n.height} ${n.x},${n.y + n.height / 2}" ${style}/>`;
    else s += `<rect x="${n.x}" y="${n.y}" width="${n.width}" height="${n.height}" rx="8" ${style}/>`;
    s += txt(n.metrics, n.textBox);
  }
  for (const e of layout.edges.filter(e => e.textBox)) s += txt(e.metrics, { ...e.textBox, x: e.textBox.x + 4, y: e.textBox.y + 4, width: e.textBox.width - 8, height: e.textBox.height - 8 });
  if (layout.title) s += txt(layout.title.metrics, layout.title, 'left');
  return s + '</svg>';
}

export async function toPng(svg, layout) {
  const { loadImage } = await import('@napi-rs/canvas');
  const scale = Math.min(2, 3600 / Math.max(layout.width, layout.height));
  const canvas = createCanvas(Math.ceil(layout.width * scale), Math.ceil(layout.height * scale));
  const context = canvas.getContext('2d');
  // The SVG rasterizer's font fallback differs from Canvas's measured fonts.
  // Rasterize paths, then draw text with the same font backend used for layout.
  const pathsOnly = svg.replace(/<text\b[^>]*>[\s\S]*?<\/text>/g, '');
  context.scale(scale, scale);
  context.drawImage(await loadImage(Buffer.from(pathsOnly)), 0, 0, layout.width, layout.height);
  const drawText = (metrics, box, align = 'center') => {
    context.font = `${metrics.fontSize}px ${FONT}`; context.fillStyle = '#263849';
    context.textBaseline = 'alphabetic'; context.textAlign = align;
    metrics.lines.forEach((line, i) => context.fillText(line,
      align === 'left' ? box.x : box.x + box.width / 2, box.y + metrics.fontSize + i * metrics.lineHeight));
  };
  for (const g of layout.groups) drawText(g.metrics, g.textBox, 'left');
  for (const n of layout.nodes) drawText(n.metrics, n.textBox);
  for (const e of layout.edges.filter(e => e.textBox)) drawText(e.metrics, {
    ...e.textBox, x: e.textBox.x + 4, y: e.textBox.y + 4, width: e.textBox.width - 8, height: e.textBox.height - 8 });
  if (layout.title) drawText(layout.title.metrics, layout.title, 'left');
  return canvas.encode('png');
}
