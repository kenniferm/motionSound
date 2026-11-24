// The display assumes spreading from point sources in a free field. The squared
// RMS contributions add as energy, since this view has no room or phase model.
function directFieldSample(sources, x, y, z, distanceSquared, maxGainSquared) {
  let energy = 0;
  for (const {center, power} of sources) {
    const dx = x - center[0], dy = y - center[1], dz = z - center[2];
    const r2 = dx*dx + dy*dy + dz*dz;
    energy += power * Math.min(maxGainSquared, distanceSquared / r2);
  }
  return energy;
}

function outwardFieldNormal(sources, point, nearDistanceSquared) {
  let x = 0, y = 0, z = 0;
  for (const {center, power} of sources) {
    const dx = point[0] - center[0], dy = point[1] - center[1], dz = point[2] - center[2];
    const r2 = dx*dx + dy*dy + dz*dz;
    if (r2 <= nearDistanceSquared) continue;
    const weight = power / (r2*r2);
    x += dx * weight;
    y += dy * weight;
    z += dz * weight;
  }
  const scale = Math.hypot(x, y, z) || 1;
  return [x/scale, y/scale, z/scale];
}

const CELL_TETRAHEDRA = [
  [0, 1, 3, 7], [0, 3, 2, 7], [0, 2, 6, 7],
  [0, 6, 4, 7], [0, 4, 5, 7], [0, 5, 1, 7],
];

function buildDirectFieldSurface(sources, level, referenceDistance, maxGain) {
  const vertices = [], faces = [];
  const totalPower = sources.reduce((sum, source) => sum + source.power, 0);
  const threshold = level * level;
  if (level <= 0 || totalPower * maxGain * maxGain <= threshold) {
    return {vertices, faces};
  }

  // At every boundary point all sources are at least this far away. That keeps
  // the sampled surface closed even when several sources augment one another.
  const margin = 1.025 * referenceDistance * Math.sqrt(totalPower) / level;
  const low = [Infinity, Infinity, Infinity], high = [-Infinity, -Infinity, -Infinity];
  for (const {center} of sources) {
    for (let axis = 0; axis < 3; axis++) {
      low[axis] = Math.min(low[axis], center[axis] - margin);
      high[axis] = Math.max(high[axis], center[axis] + margin);
    }
  }
  const span = high.map((end, axis) => end - low[axis]);
  const cells = Math.max(12, Math.min(20, Math.floor(Math.cbrt(150000 / sources.length))));
  const step = Math.max(...span) / cells;
  const [xs, ys, zs] = span.map((width, axis) => {
    const sections = Math.max(4, Math.ceil(width / step));
    return Array.from({length: sections + 1}, (_, i) => low[axis] + width * i / sections);
  });
  const row = xs.length, slice = row * ys.length;
  const samples = new Float64Array(slice * zs.length);
  const distanceSquared = referenceDistance ** 2, maxGainSquared = maxGain ** 2;
  const nearDistanceSquared = distanceSquared / maxGainSquared;
  let index = 0;
  for (const z of zs) {
    for (const y of ys) {
      for (const x of xs) {
        samples[index++] = directFieldSample(sources, x, y, z, distanceSquared, maxGainSquared);
      }
    }
  }

  const intersections = new Map();
  function gridPoint(i) {
    return [xs[i % row], ys[Math.floor(i / row) % ys.length], zs[Math.floor(i / slice)]];
  }
  function crossing(a, b) {
    const start = Math.min(a, b), end = Math.max(a, b);
    const key = start * samples.length + end;
    if (intersections.has(key)) return intersections.get(key);
    const t = (threshold - samples[start]) / (samples[end] - samples[start]);
    const p = gridPoint(start), q = gridPoint(end);
    const id = vertices.length;
    vertices.push(p.map((value, axis) => value + t * (q[axis] - value)));
    intersections.set(key, id);
    return id;
  }
  function triangle(a, b, c) {
    const center = vertices[a].map((value, i) => (value + vertices[b][i] + vertices[c][i]) / 3);
    faces.push({indices: [a, b, c], center, normal: outwardFieldNormal(sources, center, nearDistanceSquared)});
  }

  for (let z = 0; z < zs.length - 1; z++) {
    for (let y = 0; y < ys.length - 1; y++) {
      for (let x = 0; x < xs.length - 1; x++) {
        const a = x + y*row + z*slice;
        const corners = [a, a+1, a+row, a+row+1, a+slice, a+slice+1, a+slice+row, a+slice+row+1];
        const firstInside = samples[a] >= threshold;
        if (corners.every(i => (samples[i] >= threshold) === firstInside)) continue;
        for (const tetra of CELL_TETRAHEDRA) {
          const inside = [], outside = [];
          for (const corner of tetra) {
            const i = corners[corner];
            (samples[i] >= threshold ? inside : outside).push(i);
          }
          if (inside.length === 1) {
            triangle(...outside.map(i => crossing(inside[0], i)));
          } else if (inside.length === 3) {
            triangle(...inside.map(i => crossing(outside[0], i)));
          } else if (inside.length === 2) {
            const [p, q] = outside.map(i => crossing(inside[0], i));
            const [r, s] = outside.map(i => crossing(inside[1], i));
            triangle(p, q, r);
            triangle(q, s, r);
          }
        }
      }
    }
  }
  return {vertices, faces};
}

function drawDirectField(ctx, mesh, project, eye, tint) {
  if (!mesh.faces.length) return;
  const points = mesh.vertices.map(project);
  const surfaces = [];
  let hasFront = false;
  for (const {indices, center, normal} of mesh.faces) {
    const [a, b, c] = indices.map(i => points[i]);
    if (!a || !b || !c) continue;
    const dx = eye[0] - center[0], dy = eye[1] - center[1], dz = eye[2] - center[2];
    const facing = (normal[0]*dx + normal[1]*dy + normal[2]*dz) / (Math.hypot(dx, dy, dz) || 1);
    if (facing >= 0) hasFront = true;
    const light = Math.max(0, -.42*normal[0] + .79*normal[1] + .44*normal[2]);
    surfaces.push({a, b, c, facing, light, depth: (a[2]+b[2]+c[2])/3});
  }
  // When orbiting inside an envelope, show its interior.
  const visible = hasFront ? surfaces.filter(face => face.facing >= 0) : surfaces;
  visible.sort((a, b) => b.depth - a.depth);
  for (const {a, b, c, facing, light} of visible) {
    ctx.beginPath();
    ctx.moveTo(a[0], a[1]);
    ctx.lineTo(b[0], b[1]);
    ctx.lineTo(c[0], c[1]);
    ctx.closePath();
    const rim = 1 - Math.min(1, Math.abs(facing));
    const brightness = Math.round(27 + 35*light + 16*rim);
    ctx.fillStyle = `hsla(${tint}, 83%, ${brightness}%, .55)`;
    ctx.fill();
  }
}

export {directFieldSample, buildDirectFieldSurface, drawDirectField}
