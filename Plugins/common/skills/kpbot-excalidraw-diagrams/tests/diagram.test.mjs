import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { normalize, fromMermaid, mermaidBlocks, readModel } from '../scripts/model.mjs';
import { buildLayout, measure } from '../scripts/layout.mjs';
import { checkLayout, segmentIntersection } from '../scripts/geometry.mjs';
import { toScene, toSvg, toPng } from '../scripts/export.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const tmp = await fs.mkdtemp(path.join(os.tmpdir(), 'excalidraw-diagrams-test-'));
const simple = () => normalize({ nodes: [{ id: 'a', label: '入口' }, { id: 'b', label: '业务处理' }], edges: [{ from: 'a', to: 'b' }] });

test('schema rejects lost relationships, unknown fields and group cycles', () => {
  assert.throws(() => normalize({ nodes: [{ id: 'a', label: 'A' }], edges: [{ from: 'a', to: 'missing' }] }), /existing leaf/);
  assert.throws(() => normalize({ nodes: [{ id: 'a', label: 'A', lable: 'B' }], edges: [] }), /unknown field/);
  assert.throws(() => normalize({ nodes: [{ id: 'a', label: 'A' }], edges: [], groups: [{ id: 'g', label: 'G', group: 'g' }] }), /cyclic/);
  assert.throws(() => normalize({ nodes: [{ id: '__root__', label: 'A' }], edges: [] }), /reserved/);
});

test('Mermaid preserves labels, direction, fan-out and arrow semantics', async () => {
  const graph = await fromMermaid('flowchart TB\nA[入口] <--> B{完成?}\nB -.待处理.-> C[队列]\nC --- D[说明]');
  assert.equal(graph.direction, 'TB'); assert.equal(graph.nodes.length, 4); assert.equal(graph.edges.length, 3);
  assert.deepEqual(graph.edges.map(e => e.heads), ['both', 'end', 'none']);
  assert.equal(graph.edges[1].style, 'dashed'); assert.equal(graph.edges[1].label, '待处理');
});

test('Mermaid flowcharts require an explicit direction in the header', async () => {
  await assert.rejects(fromMermaid('flowchart\nA-->B'), /direction .*"flowchart LR"/);
  await assert.rejects(fromMermaid('%% leading comment\r\ngraph\r\nLR-->B'), /direction .*"graph LR"/);
});

test('unsupported diagrams and active/invisible syntax are not silently reinterpreted', async () => {
  await assert.rejects(fromMermaid('sequenceDiagram\nA->>B:请求'), /supports Mermaid flowchart\/graph only/);
  await assert.rejects(fromMermaid('flowchart LR\nA-->B\nclick A "https://example.com"'), /directives/);
  await assert.rejects(fromMermaid('flowchart LR\nA~~~B'), /invisible/);
});

test('multiple Markdown blocks require explicit selection; prose is not guessed by the CLI', async () => {
  const md = '# 文档\n```mermaid\nflowchart LR\nA-->B\n```\n~~~mermaid\nflowchart TB\nC-->D\n~~~';
  assert.equal(mermaidBlocks(md).length, 2);
  const filename = path.join(tmp, 'notes.md'); await fs.writeFile(filename, md);
  await assert.rejects(readModel(filename), /Found 2/);
  assert.equal((await readModel(filename, 2)).direction, 'TB');
  await fs.writeFile(filename, '# 业务需求\n入口发送数据到服务。');
  await assert.rejects(readModel(filename), /graph spec/);
});

test('CLI --block 1 selects the first diagram after a fenced Markdown example', async () => {
  const input = path.join(tmp, 'fenced-example.md');
  await fs.writeFile(input, [
    '````markdown', '```mermaid', 'flowchart TB', 'Example-->Only', '```', '````',
    '~~~mermaid', 'flowchart LR', 'Real-->Diagram', '~~~',
    '```mermaid', 'flowchart TB', 'Second-->Diagram', '```',
  ].join('\n'));
  const output = path.join(tmp, 'first-diagram.excalidraw');
  const result = spawnSync(process.execPath, [path.join(root, 'scripts/diagram.mjs'),
    '--input', input, '--block', '1', '--output', output], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  const graph = JSON.parse(await fs.readFile(path.join(tmp, 'first-diagram.graph.json'), 'utf8'));
  assert.deepEqual(graph.nodes.map(node => node.id), ['Real', 'Diagram']);
});

test('Markdown fences close only on the same marker of sufficient length', () => {
  const markdown = [
    '  ~~~~', '~~~', '`````', '~~~~ text', '```mermaid', 'hidden', '```', '  ~~~~~\t',
    '  ````mermaid', '~~~', '```', '````` text', '  `````  ',
    '~~~mermaid', 'visible', '~~~~',
    '```text', '~~~mermaid', 'inside an unclosed ordinary fence', '~~~',
  ].join('\r\n');
  assert.deepEqual(mermaidBlocks(markdown), ['~~~\n```\n````` text', 'visible']);
  assert.throws(() => mermaidBlocks('````mermaid\nignored\n```'), /Unclosed Mermaid fence/);
});

test('CJK labels are measured with a real CJK font and long text grows the node', async () => {
  assert.ok(measure('中文测试', 20).width >= 80);
  const short = await buildLayout(simple());
  const model = simple(); model.nodes[0].label = '这是需要完整保留的中文长标签，包含输入来源、处理条件与输出约束。'.repeat(3);
  const long = await buildLayout(model);
  assert.ok(long.layout.nodes[0].height > short.layout.nodes[0].height);
  assert.equal(long.qa.pass, true, JSON.stringify(long.qa.errors));
});

for (const direction of ['LR', 'RL', 'TB', 'BT']) test(`clear layout respects ${direction}`, async () => {
  const model = simple(); model.direction = direction;
  const { layout, qa } = await buildLayout(model);
  assert.equal(qa.pass, true, JSON.stringify(qa.errors));
  const [a, b] = layout.nodes;
  assert.ok(direction === 'LR' ? a.x < b.x : direction === 'RL' ? a.x > b.x : direction === 'TB' ? a.y < b.y : a.y > b.y);
});

test('nested groups, cycles, self-loops and fan-out route without collisions', async () => {
  for (const text of [
    'flowchart LR\nsubgraph G[平台]\nsubgraph H[服务]\nA[网关] --> B[服务]\nend\nB --> C[数据]\nend\nC --> D[结果]',
    'flowchart LR\nA[校验] --> B[处理]\nB --> C[结果]\nC -->|重试| A',
    'flowchart LR\nA[轮询] --> A',
    'flowchart LR\nA[输入] --> B[任务一]\nA --> C[任务二]\nA --> D[任务三]\nB --> E[汇总]\nC --> E\nD --> E',
  ]) {
    const { qa } = await buildLayout(await fromMermaid(text));
    assert.equal(qa.pass, true, JSON.stringify(qa.errors));
  }
});

test('checker catches containment of unrelated nodes, not just partial overlap', async () => {
  const { layout } = await buildLayout(simple());
  const [a, b] = layout.nodes; b.x = a.x + 20; b.y = a.y + 20; b.width = 40; b.height = 30;
  assert.ok(checkLayout(layout).errors.some(e => e.kind === 'node_overlap_or_gap'));
});

test('group headers avoid incoming branch arrows in the reserved title band', async () => {
  const model = await readModel(path.join(root, 'tests/fixtures/grouped-branches.json'));
  const { qa } = await buildLayout(model);
  assert.equal(qa.pass, true, JSON.stringify(qa.errors));
  assert.deepEqual(qa.counts, { nodes: 7, edges: 7, groups: 2 });
});

test('checker detects an arrow passing through an unrelated node or label', async () => {
  const { layout } = await buildLayout(simple());
  const [p, q] = layout.edges[0].points;
  layout.nodes.push({ id: 'obstacle', shape: 'rectangle', x: (p.x + q.x) / 2 - 20, y: p.y - 20, width: 40, height: 40,
    textBox: { x: (p.x + q.x) / 2 - 5, y: p.y - 5, width: 10, height: 10 } });
  const errors = checkLayout(layout).errors;
  assert.ok(errors.some(e => e.kind === 'edge_through_node'));
  assert.ok(errors.some(e => e.kind === 'edge_through_label'));
});

test('checker distinguishes crossing and coincident arrow segments', () => {
  assert.equal(segmentIntersection({ x: 0, y: 10 }, { x: 100, y: 10 }, { x: 30, y: 0 }, { x: 30, y: 50 }).type, 'crossing');
  assert.equal(segmentIntersection({ x: 0, y: 10 }, { x: 100, y: 10 }, { x: 30, y: 10 }, { x: 80, y: 10 }).type, 'shared_segment');
});

test('native scene keeps every node/edge, editable text and reciprocal bindings', async () => {
  const model = await readModel(path.join(root, 'tests/fixtures/chinese-flow.mmd'));
  const { layout, qa } = await buildLayout(model); assert.equal(qa.pass, true);
  const scene = toScene(layout, model), byId = new Map(scene.elements.map(e => [e.id, e]));
  assert.equal(scene.type, 'excalidraw'); assert.equal(scene.elements.filter(e => e.customData?.role === 'node').length, model.nodes.length);
  assert.equal(scene.elements.filter(e => e.type === 'arrow').length, model.edges.length);
  for (const edge of scene.elements.filter(e => e.type === 'arrow')) for (const key of ['startBinding', 'endBinding']) {
    assert.ok(byId.get(edge[key].elementId).boundElements.some(bound => bound.id === edge.id));
  }
  assert.deepEqual(scene.elements.filter(e => e.customData?.role === 'node-label').map(e => e.originalText).sort(), model.nodes.map(n => n.label).sort());
  const svg = toSvg(layout), png = await toPng(svg, layout);
  assert.equal(png.subarray(1, 4).toString(), 'PNG'); assert.ok(png.length > 2000);
  assert.ok(svg.includes('用户输入')); assert.ok(!svg.includes('<script'));
});

test('non-planar graph fails strict delivery while preserving its relationships', async () => {
  const model = normalize({ nodes: ['a', 'b', 'c', 'x', 'y', 'z'].map(id => ({ id, label: id })),
    edges: ['a', 'b', 'c'].flatMap(from => ['x', 'y', 'z'].map(to => ({ from, to }))) });
  const input = path.join(tmp, 'dense.json'), output = path.join(tmp, 'dense.excalidraw');
  await fs.writeFile(input, JSON.stringify(model));
  const process = spawnSync(globalThis.process.execPath, [path.join(root, 'scripts/diagram.mjs'), '--input', input, '--output', output], { encoding: 'utf8' });
  assert.equal(process.status, 2, process.stderr);
  const qa = JSON.parse(await fs.readFile(path.join(tmp, 'dense.qa.json')));
  assert.equal(qa.pass, false); assert.equal(qa.exportedEdges, 9);
  assert.ok(qa.errors.some(e => e.kind === 'edge_crossing'));
});

test('CLI refuses to overwrite a source or existing output', () => {
  const cli = path.join(root, 'scripts/diagram.mjs');
  const input = path.join(tmp, 'dense.json'), output = path.join(tmp, 'dense.excalidraw');
  const process = spawnSync(globalThis.process.execPath, [cli, '--input', input, '--output', output], { encoding: 'utf8' });
  assert.equal(process.status, 1); assert.match(process.stderr, /Output exists/);
});

test.after(async () => { await fs.rm(tmp, { recursive: true, force: true }); });
