const assert = require('node:assert/strict');
const {test} = require('node:test');
const {directFieldSample, buildDirectFieldSurface} = require('../frontend/src/spatial-field.js');

function source(center, rms) {
  return {center, power: rms ** 2};
}

function connectedParts(mesh) {
  const adjacency = mesh.vertices.map(() => []);
  for (const {indices: [a, b, c]} of mesh.faces) {
    adjacency[a].push(b, c);
    adjacency[b].push(a, c);
    adjacency[c].push(a, b);
  }
  const visited = new Set();
  let parts = 0;
  for (let i = 0; i < adjacency.length; i++) {
    if (visited.has(i)) continue;
    parts++;
    const queue = [i];
    visited.add(i);
    while (queue.length) {
      for (const neighbor of adjacency[queue.pop()]) {
        if (visited.has(neighbor)) continue;
        visited.add(neighbor);
        queue.push(neighbor);
      }
    }
  }
  return parts;
}

test('direct energy follows the point-source distance gain, including the near cap', () => {
  const sources = [source([3, -2, 1], .6)];
  const at = distance => directFieldSample(sources, 3, -2, 1 + distance, 1.7**2, 2**2);
  assert.ok(Math.abs(at(0) - 1.44) < 1e-10);
  assert.ok(Math.abs(at(1.7) - .36) < 1e-10);
  assert.ok(Math.abs(at(3.4) - .09) < 1e-10);
});

test('one source produces a closed surface around its actual three-dimensional location', () => {
  const center = [3, -2, 1];
  const mesh = buildDirectFieldSurface([source(center, .6)], .15, 1.7, 2);
  assert.ok(mesh.faces.length > 0);
  for (const vertex of mesh.vertices) {
    const radius = Math.hypot(...vertex.map((coordinate, i) => coordinate - center[i]));
    assert.ok(Math.abs(radius - 6.8) < .12, radius);
  }
  const edges = new Map();
  for (const {indices: [a, b, c]} of mesh.faces) {
    for (const [from, to] of [[a, b], [b, c], [c, a]]) {
      const key = Math.min(from, to) + ':' + Math.max(from, to);
      edges.set(key, (edges.get(key) ?? 0) + 1);
    }
  }
  assert.ok(Array.from(edges.values()).every(count => count === 2));
});

test('overlapping energy can connect two ranges while silence has no surface', () => {
  const pair = [source([-4, 0, 0], .4), source([4, 0, 0], .4)];
  assert.equal(connectedParts(buildDirectFieldSurface(pair, .30, 1.7, 2)), 2);
  assert.equal(connectedParts(buildDirectFieldSurface(pair, .20, 1.7, 2)), 1);
  assert.equal(buildDirectFieldSurface([source([0, 0, 0], 0)], .15, 1.7, 2).faces.length, 0);
});
