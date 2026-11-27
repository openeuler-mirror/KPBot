import path from 'node:path';
import { readBoundedText } from './input.mjs';

const directions = new Set(['LR', 'RL', 'TB', 'BT']);
const shapes = new Set(['rectangle', 'ellipse', 'diamond']);
const fail = message => { throw new Error(message); };
const keys = (object, allowed, context) => {
  if (!object || typeof object !== 'object' || Array.isArray(object)) fail(`${context}: expected object`);
  for (const key of Object.keys(object)) if (!allowed.includes(key)) fail(`${context}: unknown field ${key}`);
};
const label = (value, context) => {
  if (typeof value !== 'string' || !value.trim() || value.length > 2000) fail(`${context}: label must contain 1–2000 characters`);
  return value.replaceAll('\r\n', '\n');
};
export function normalize(raw) {
  keys(raw, ['title', 'direction', 'nodes', 'edges', 'groups', 'warnings', 'source'], 'graph');
  if (raw.warnings !== undefined && (!Array.isArray(raw.warnings) || raw.warnings.some(w => typeof w !== 'string'))) fail('warnings must be a string array');
  if (raw.source !== undefined && typeof raw.source !== 'string') fail('source must be a string');
  const graph = { title: raw.title ? label(raw.title, 'title') : '', direction: raw.direction ?? 'LR',
    nodes: [], edges: [], groups: [], warnings: [...(raw.warnings ?? [])], source: raw.source ?? 'graph-spec' };
  if (!directions.has(graph.direction)) fail('direction must be LR, RL, TB or BT');
  if (!Array.isArray(raw.nodes) || raw.nodes.length < 1 || raw.nodes.length > 80) fail('Use 1–80 nodes per view; split a larger graph');
  if (!Array.isArray(raw.edges) || raw.edges.length > 160) fail('edges must be an array with at most 160 entries');
  if (!Array.isArray(raw.groups ?? []) || (raw.groups ?? []).length > 20) fail('Use at most 20 groups');
  const ids = new Set();
  const unique = id => {
    if (typeof id !== 'string' || !id.trim() || id.length > 100 || ids.has(id) || id === '__root__' || id.startsWith('port_')) fail(`Invalid, reserved or duplicate ID: ${id}`);
    ids.add(id); return id;
  };
  for (const g of raw.groups ?? []) {
    keys(g, ['id', 'label', 'group'], 'group');
    graph.groups.push({ id: unique(g.id), label: label(g.label, g.id), ...(g.group ? { group: g.group } : {}) });
  }
  for (const n of raw.nodes) {
    keys(n, ['id', 'label', 'shape', 'group', 'role', 'sourceShape'], 'node');
    const node = { id: unique(n.id), label: label(n.label, n.id), shape: n.shape ?? 'rectangle',
      ...(n.group ? { group: n.group } : {}), ...(n.role ? { role: n.role } : {}), ...(n.sourceShape ? { sourceShape: n.sourceShape } : {}) };
    if (!shapes.has(node.shape)) fail(`${node.id}: shape must be rectangle, ellipse or diamond`);
    graph.nodes.push(node);
  }
  const groups = new Map(graph.groups.map(g => [g.id, g]));
  for (const n of [...graph.nodes, ...graph.groups]) {
    const visited = new Set([n.id]); let parent = n.group;
    while (parent) {
      if (!groups.has(parent) || visited.has(parent)) fail(`${n.id}: invalid or cyclic group membership`);
      visited.add(parent); parent = groups.get(parent).group;
    }
  }
  const nodeIds = new Set(graph.nodes.map(n => n.id));
  for (const [i, e] of raw.edges.entries()) {
    keys(e, ['id', 'from', 'to', 'label', 'style', 'heads'], 'edge');
    if (!nodeIds.has(e.from) || !nodeIds.has(e.to)) fail(`Edge ${i}: endpoints must be existing leaf nodes; group endpoints need an explicit boundary node`);
    const edge = { id: unique(e.id ?? `edge-${i + 1}`), from: e.from, to: e.to,
      label: e.label ? label(e.label, `edge ${i}`) : '', style: e.style ?? 'solid', heads: e.heads ?? 'end' };
    if (!['solid', 'dashed', 'thick'].includes(edge.style)) fail(`${edge.id}: invalid edge style`);
    if (!['end', 'both', 'none'].includes(edge.heads)) fail(`${edge.id}: invalid arrowheads`);
    graph.edges.push(edge);
  }
  return graph;
}

let parser;
async function mermaidParser() {
  if (parser) return parser;
  const { JSDOM } = await import('jsdom');
  // No resource loader, runScripts or browser navigation: parsing stays offline.
  const dom = new JSDOM('');
  globalThis.window = dom.window; globalThis.document = dom.window.document;
  parser = (await import('mermaid')).default;
  parser.initialize({ startOnLoad: false, securityLevel: 'strict', suppressErrorRendering: true,
    maxTextSize: 60000, flowchart: { htmlLabels: false }, themeVariables: { fontFamily: 'Arial' } });
  return parser;
}

export async function fromMermaid(source, title = '') {
  if (source.length > 60000) fail('Mermaid input exceeds 60,000 characters; split it');
  if (/%%\{|^\s*---|^\s*click\s/m.test(source)) fail('Remove Mermaid init/config/click directives; only declarative diagram content is accepted');
  const header = /^\s*(?:%%[^\n]*\n\s*)*(flowchart|graph)\b(?:[ \t]+(LR|RL|TB|TD|BT)\b)?/.exec(source);
  if (!header) {
    fail('Automatic conversion supports Mermaid flowchart/graph only. Do not flatten sequence, ER, class, state or timing semantics into a flowchart. See references/input-contract.md');
  }
  if (!header[2]) {
    fail(`Specify a Mermaid direction (LR, RL, TB, TD or BT) in the header, for example "${header[1]} LR".`);
  }
  const mermaid = await mermaidParser();
  const diagram = await mermaid.mermaidAPI.getDiagramFromText(source);
  if (!diagram.type.startsWith('flowchart')) fail(`Unsupported Mermaid diagram type: ${diagram.type}`);
  const warnings = [], nodes = [], groups = [], edges = [];
  const subgroup = diagram.db.getSubGraphs();
  const groupIds = new Set(subgroup.map(g => g.id));
  const parent = new Map();
  for (const g of subgroup) for (const id of g.nodes) {
    if (parent.has(id) && parent.get(id) !== g.id) fail(`${id}: multiple subgraph parents are not supported`);
    parent.set(id, g.id);
  }
  const plain = text => String(text).replace(/<br\s*\/?\s*>/gi, '\n').replaceAll('&quot;', '"').replaceAll('&amp;', '&');
  for (const g of subgroup) {
    groups.push({ id: g.id, label: plain(g.title || g.id), ...(parent.has(g.id) ? { group: parent.get(g.id) } : {}) });
    if (g.dir) warnings.push(`Subgraph ${g.id}: its local direction is normalized to the graph direction; membership is preserved.`);
  }
  const shapeMap = { square: 'rectangle', rect: 'rectangle', round: 'rectangle', stadium: 'rectangle',
    subroutine: 'rectangle', cylinder: 'rectangle', circle: 'ellipse', ellipse: 'ellipse',
    doublecircle: 'ellipse', diamond: 'diamond', question: 'diamond' };
  for (const [id, v] of diagram.db.getVertices()) {
    if (groupIds.has(id)) continue;
    const original = v.type || 'square';
    if (!(original in shapeMap)) fail(`${id}: unsupported Mermaid shape ${original}; normalize its semantics explicitly into a graph spec`);
    if (v.labelType === 'markdown' || /<[^>]+>/.test(plain(v.text || id))) fail(`${id}: rich labels are not supported; provide equivalent plain text`);
    const mapped = shapeMap[original];
    if (!['square', 'rect', 'circle', 'ellipse', 'diamond', 'question'].includes(original)) {
      warnings.push(`${id}: source shape ${original} is represented by editable ${mapped}; original shape is retained in sourceShape.`);
    }
    nodes.push({ id, label: plain(v.text || id), shape: mapped, sourceShape: original,
      ...(original === 'cylinder' ? { role: 'store' } : {}), ...(parent.has(id) ? { group: parent.get(id) } : {}) });
    if (v.styles?.length || v.classes?.length || Object.keys(v.props ?? {}).length) warnings.push(`${id}: styling is normalized to the skill palette.`);
  }
  for (const [i, e] of diagram.db.getEdges().entries()) {
    if (!['normal', 'dotted', 'thick'].includes(e.stroke)) fail(`Edge ${i}: unsupported Mermaid stroke ${e.stroke}; do not turn invisible layout links into relationships`);
    if (!['arrow_point', 'double_arrow_point', 'arrow_open'].includes(e.type)) fail(`Edge ${i}: unsupported Mermaid arrow type ${e.type}; preserve it explicitly before conversion`);
    edges.push({ id: `mermaid-edge-${i + 1}`, from: e.start, to: e.end, label: plain(e.text || ''),
      style: e.stroke === 'dotted' ? 'dashed' : e.stroke === 'thick' ? 'thick' : 'solid',
      heads: e.type === 'arrow_open' ? 'none' : e.type === 'double_arrow_point' ? 'both' : 'end' });
  }
  return normalize({ title, direction: diagram.db.getDirection().replace('TD', 'TB'), nodes, edges, groups, warnings, source: 'mermaid' });
}

export function mermaidBlocks(markdown) {
  const blocks = [];
  let current = null;
  for (const line of markdown.split(/\r?\n/)) {
    if (!current) {
      const open = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
      if (!open) continue;
      const [, fence, info] = open;
      if (fence[0] === '`' && info.includes('`')) continue;
      current = { fence, lines: info.trim() === 'mermaid' ? [] : null };
    } else {
      const close = /^ {0,3}(`{3,}|~{3,})[ \t]*$/.exec(line);
      if (close && close[1].startsWith(current.fence)) {
        if (current.lines) blocks.push(current.lines.join('\n'));
        current = null;
      } else {
        current.lines?.push(line);
      }
    }
  }
  if (current?.lines) fail('Unclosed Mermaid fence');
  return blocks;
}

export async function readModel(input, block, title) {
  const text = await readBoundedText(input);
  const ext = path.extname(input).toLowerCase();
  if (ext === '.json') {
    const graph = normalize(JSON.parse(text));
    if (title) graph.title = label(title, 'title');
    return graph;
  }
  if (ext === '.md' || ext === '.markdown') {
    const blocks = mermaidBlocks(text);
    if (!blocks.length) fail('No Mermaid block. First extract an evidence-backed graph spec from the Markdown prose; see references/input-contract.md');
    if (blocks.length > 1 && !block) fail(`Found ${blocks.length} Mermaid blocks. Convert each separately with --block 1, --block 2, etc.; do not drop blocks`);
    if (block && (block < 1 || block > blocks.length)) fail('Mermaid block index out of range');
    return fromMermaid(blocks[(block ?? 1) - 1], title);
  }
  if (!['.mmd', '.mermaid', '.txt'].includes(ext)) fail('Input must be .json, .md, .mmd, .mermaid or .txt');
  return fromMermaid(text, title);
}
