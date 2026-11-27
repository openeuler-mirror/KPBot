#!/usr/bin/env node
import path from 'node:path';
import { parseArgs } from 'node:util';
import { readModel } from './model.mjs';
import { buildLayout } from './layout.mjs';
import { toScene, toSvg, toPng } from './export.mjs';
import { planOutputs, publishOutputs } from './output.mjs';

async function main() {
  const { values } = parseArgs({ options: {
    input: { type: 'string' }, output: { type: 'string' }, title: { type: 'string' },
    block: { type: 'string' }, force: { type: 'boolean', default: false }, help: { type: 'boolean' } } });
  if (values.help) {
    console.log('node diagram.mjs --input graph.json|diagram.mmd|notes.md --output diagram.excalidraw [--title TITLE] [--block 1] [--force]\nCreates native scene, SVG, PNG, normalized graph and QA JSON. Exit 2 = unresolved layout collisions; do not deliver as approved.');
    return;
  }
  if (!values.input || !values.output || !values.output.endsWith('.excalidraw')) throw new Error('--input and --output NAME.excalidraw are required');
  const output = path.resolve(values.output), stem = output.slice(0, -'.excalidraw'.length);
  const files = [output, `${stem}.svg`, `${stem}.png`, `${stem}.graph.json`, `${stem}.qa.json`];
  const input = path.resolve(values.input);
  const outputs = await planOutputs(input, files, values.force);
  const block = values.block === undefined ? undefined : Number(values.block);
  if (block !== undefined && (!Number.isInteger(block) || block < 1)) throw new Error('--block must be a positive integer');
  const model = await readModel(input, block, values.title);
  const { layout, qa } = await buildLayout(model);
  const scene = toScene(layout, model), svg = toSvg(layout), png = await toPng(svg, layout);
  qa.input = input; qa.output = output;
  qa.rendering = 'SVG/PNG are local geometry previews, not screenshots of the Excalidraw editor. Inspect the preview; inspect the native editor when available.';
  qa.sourceNodeIds = model.nodes.map(n => n.id); qa.sourceEdgeIds = model.edges.map(e => e.id);
  qa.exportedNodes = scene.elements.filter(e => e.customData?.role === 'node').length;
  qa.exportedEdges = scene.elements.filter(e => e.customData?.role === 'edge').length;
  if (qa.exportedNodes !== model.nodes.length || qa.exportedEdges !== model.edges.length) throw new Error('Export lost graph content');
  const data = [JSON.stringify(scene, null, 2), svg, png, JSON.stringify(model, null, 2), JSON.stringify(qa, null, 2)];
  await publishOutputs(outputs, data);
  console.log(JSON.stringify({ pass: qa.pass, output, preview: `${stem}.png`, qa: `${stem}.qa.json`, counts: qa.counts,
    errors: qa.errors, warnings: qa.warnings }, null, 2));
  if (!qa.pass) process.exitCode = 2;
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
