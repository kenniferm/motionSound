import {buildDirectFieldSurface, drawDirectField} from './spatial-field.js';

function position(keys, frame) {
  let low = 0;
  let high = keys.length - 1;
  while (high - low > 1) {
    const middle = (low + high) >> 1;
    if (keys[middle][0] <= frame) low = middle;
    else high = middle;
  }
  const a = keys[low];
  const b = keys[high];
  const t = a === b ? 0 : Math.max(0, Math.min(1, (frame - a[0]) / (b[0] - a[0])));
  const dot = Math.max(-1, Math.min(1, a[1]*b[1] + a[2]*b[2] + a[3]*b[3]));
  const angle = Math.acos(dot);
  const arc = Math.sin(angle);
  const curved = Math.abs(arc) > 1e-6;
  const wa = curved ? Math.sin((1-t)*angle) / arc : 1-t;
  const wb = curved ? Math.sin(t*angle) / arc : t;
  const distance = a[4] + t*(b[4] - a[4]);
  const x = wa*a[1] + wb*b[1];
  const y = wa*a[2] + wb*b[2];
  const z = wa*a[3] + wb*b[3];
  const scale = distance / (Math.hypot(x, y, z) || 1);
  return [-y*scale, z*scale, -x*scale]; // right, up, back
}

function hue(id) {
  let hash = 0;
  for (const c of id) hash = (Math.imul(hash, 31) + c.codePointAt(0)) >>> 0;
  return (hash * 137.508) % 360;
}

function timeText(seconds) {
  const whole = Math.floor(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`;
}

const WAVE_RATE = 50;
const TRACE_RATE = 1000;
const TRACE_WINDOW = .064;
const TRACE_DEPTH = .034;
const FIELD_FPS = 25;

function hasRecentSignal(trace, seconds) {
  const end = Math.floor(seconds * TRACE_RATE);
  const start = Math.max(0, end - Math.ceil((TRACE_WINDOW + TRACE_DEPTH) * TRACE_RATE));
  for (let i = start * 2; i < Math.min(end * 2, trace.length); i++) {
    if (trace[i] !== 0) return true;
  }
  return false;
}

function envelopeAt(bins, seconds) {
  if (seconds < 0) return 0;
  const sample = seconds * WAVE_RATE;
  const index = Math.floor(sample);
  const a = bins[index] ?? 0;
  const b = bins[index + 1] ?? a;
  return (a + (b - a) * (sample - index)) / 255;
}

function sourcePowerAt(source, seconds) {
  const bin = Math.floor(seconds * WAVE_RATE) - 1;
  const current = (source.wave[bin] ?? 0) / 255;
  const prior = (source.wave[bin - 1] ?? 0) / 255;
  // These bins store the square root of RMS. Use complete bins only.
  return (current ** 4 + prior ** 4) / 2;
}

function isSelected(source, selected) {
  return !selected || source.id === selected || '$' + source.formation?.id === selected;
}

function traceAt(trace, seconds) {
  const frame = seconds * TRACE_RATE - .5;
  const index = Math.floor(frame);
  const mix = frame - index;
  return [0, 1].map(channel => {
    const left = trace[index * 2 + channel] ?? 0;
    const right = trace[(index + 1) * 2 + channel] ?? 0;
    return (left + (right - left) * mix) / 127;
  });
}

function drawSourceWave(ctx, project, marker, extent, columns) {
  const {center, source, seconds, level, bass, dim} = marker;
  const [x, y, z] = center;
  const span = extent * (.27 + .16 * level);
  const left = project([x - span, y, z]);
  const right = project([x + span, y, z]);
  const color = `hsl(${source.hue} 85% 73%)`;
  const midline = bass ? '#ffcc6c' : color;
  function fadedStroke(stroke) {
    if (!left || !right || Math.hypot(left[0] - right[0], left[1] - right[1]) < 1) return stroke;
    const gradient = ctx.createLinearGradient(left[0], left[1], right[0], right[1]);
    gradient.addColorStop(0, 'transparent');
    gradient.addColorStop(.23, stroke);
    gradient.addColorStop(.77, stroke);
    gradient.addColorStop(1, 'transparent');
    return gradient;
  }
  function path(points) {
    let connected = false;
    for (const point of points) {
      if (!point) {
        connected = false;
        continue;
      }
      if (connected) ctx.lineTo(point[0], point[1]);
      else ctx.moveTo(point[0], point[1]);
      connected = true;
    }
  }
  const layers = fadedStroke(color);
  const centerStroke = fadedStroke(midline);
  const emphasis = dim ? .28 : 1;
  const rows = 14;
  for (let row = 0; row <= rows; row++) {
    const depth = 2 * row / rows - 1;
    const central = row === rows / 2;
    // Each slice shows the preceding 64 ms. Deeper slices are earlier in the clip.
    const end = (Math.floor(seconds * TRACE_RATE) - .5) / TRACE_RATE - (1 - row / rows) * TRACE_DEPTH;
    const ridge = [], lower = [], upper = [];
    for (let i = 0; i <= columns; i++) {
      const u = i / columns;
      const [min, max] = traceAt(source.trace, end - (1 - u) * TRACE_WINDOW);
      const wx = x + (2 * u - 1) * span;
      const wz = z + depth * span * .62;
      const point = value => project([wx, y + extent * .52 * value, wz]);
      ridge.push(point((min + max) / 2));
      if (central) {
        lower.push(point(min));
        upper.push(point(max));
      }
    }
    if (central && lower.every(Boolean) && upper.every(Boolean)) {
      // Within-bin peaks remain visible even when a fast oscillation averages to zero.
      ctx.beginPath();
      path(upper);
      const reverse = lower.reverse();
      for (const point of reverse) ctx.lineTo(point[0], point[1]);
      ctx.closePath();
      ctx.fillStyle = centerStroke;
      ctx.globalAlpha = .28 * emphasis;
      ctx.fill();
    }
    ctx.beginPath();
    path(ridge);
    ctx.globalAlpha = emphasis * (central ? .93 : .14 + .33 * (1 - Math.abs(depth)));
    ctx.lineWidth = central ? 1.6 : .9;
    ctx.strokeStyle = central ? centerStroke : layers;
    ctx.stroke();
  }
  ctx.globalAlpha = 1;
  ctx.lineWidth = 1;
}

export function bindSpatialViewer(dialog, audio, data) {
  const canvas = dialog.querySelector('canvas');
  const ctx = canvas.getContext('2d');
  const find = name => dialog.querySelector('.' + name);
  const play = find('play'), seek = find('seek'), volume = find('volume');
  const select = find('source'), timeLabel = find('time');
  const view = find('view'), fieldControls = find('field-controls'), fieldLevel = find('field-level');
  const controls = new AbortController();
  const on = (element, event, callback) => {
    element.addEventListener(event, callback, {signal: controls.signal});
  };
  let sources = [], rate = 48000, extent = 1, frameId = 0;
  let formations = new Map();
  let yaw = .35, pitch = .35, zoom = 1, pointer = null;
  let referencePower = 0, referenceDistance, maxDirectGain, fieldCache, lastFieldPaint;

  function fieldAt(time, selected) {
    const tick = Math.floor(time * FIELD_FPS + 1e-7);
    const level = Number(fieldLevel.value);
    if (fieldCache?.tick === tick && fieldCache.selected === selected && fieldCache.level === level) {
      return fieldCache;
    }
    const frame = tick * rate / FIELD_FPS;
    const contributing = [];
    for (const source of sources) {
      if (!isSelected(source, selected)) continue;
      const local = frame - source.start;
      if (local < 0 || local >= source.frames) continue;
      const power = sourcePowerAt(source, local / rate);
      if (power > 0) contributing.push({center: position(source.keys, local), power});
    }
    const threshold = Math.sqrt(referencePower) * 10 ** (level / 20);
    const mesh = buildDirectFieldSurface(contributing, threshold, referenceDistance, maxDirectGain);
    fieldCache = {tick, frame, selected, level, mesh};
    return fieldCache;
  }

  function draw() {
    if (!audio || !dialog.open || !ctx) return;
    const width = canvas.clientWidth, height = canvas.clientHeight;
    if (!width || !height) return;
    const now = audio.currentTime;
    const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
    seek.max = duration;
    seek.value = now;
    timeLabel.textContent = `${timeText(now)} / ${timeText(duration)}`;
    const selected = select.value;
    const field = view.value === 'field' ? fieldAt(now, selected) : null;
    const pixelRatio = Math.min(2, window.devicePixelRatio || 1);
    const paintKey = `${width}:${height}:${pixelRatio}:${yaw}:${pitch}:${zoom}`;
    if (field && lastFieldPaint?.field === field && lastFieldPaint.key === paintKey) return;
    lastFieldPaint = field ? {field, key: paintKey} : null;
    const pixelsWide = Math.round(width * pixelRatio), pixelsHigh = Math.round(height * pixelRatio);
    if (canvas.width !== pixelsWide || canvas.height !== pixelsHigh) {
      canvas.width = pixelsWide;
      canvas.height = pixelsHigh;
    }
    ctx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const distance = extent * 3.4 / zoom;
    const focal = Math.min(width, height) * 1.18;
    const cy = Math.cos(yaw), sy = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
    function project([x, y, z]) {
      const back = z*cy - x*sy;
      const depth = distance - (y*sp + back*cp);
      if (depth <= extent * .04) return null;
      return [width/2 + (x*cy + z*sy)*focal/depth,
              height/2 - (y*cp - back*sp)*focal/depth, depth];
    }
    const eye = [-sy*cp*distance, sp*distance, cy*cp*distance];
    drawPositions(project, field?.frame ?? now * rate, selected, width, focal, field?.mesh, eye);
  }

  function drawPositions(project, now, selected, width, focal, mesh, eye) {
    function line(a, b, color, opacity = 1) {
      if (!a || !b) return;
      ctx.globalAlpha = opacity;
      ctx.strokeStyle = color;
      ctx.beginPath();
      ctx.moveTo(a[0], a[1]);
      ctx.lineTo(b[0], b[1]);
      ctx.stroke();
      ctx.globalAlpha = 1;
    }
    function dot(point, radius, color) {
      if (!point) return;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(point[0], point[1], radius, 0, Math.PI*2);
      ctx.fill();
    }
    for (let i = -4; i <= 4; i++) {
      const x = extent * i/4;
      const color = i ? '#253342' : '#526779';
      line(project([x, 0, -extent]), project([x, 0, extent]), color);
      line(project([-extent, 0, x]), project([extent, 0, x]), color);
    }
    const origin = project([0, 0, 0]);
    line(origin, project([0, extent, 0]), '#526779');
    if (mesh) {
      const tint = selected ? hue(selected.startsWith('$') ? selected.slice(1) : selected) : 200;
      drawDirectField(ctx, mesh, project, eye, tint);
    }
    ctx.font = '13px system-ui';
    ctx.fillStyle = '#adc2d5';
    for (const [text, x, y, z] of [
      ['前', 0, 0, -1], ['後', 0, 0, 1], ['左', -1, 0, 0],
      ['右', 1, 0, 0], ['上', 0, 1, 0], ['下', 0, -1, 0],
    ]) {
      const p = project([x*extent, y*extent, z*extent]);
      if (p) ctx.fillText(text, p[0]+5, p[1]+3);
    }
    const markers = [{point: origin, radius: 6, color: '#edf7ff', label: '聴き手'}];
    const active = new Map();
    for (const source of sources) {
      const local = now - source.start;
      if (local >= 0 && local < source.frames) {
        const center = position(source.keys, local);
        const point = project(center);
        if (point) active.set(source.id, {point, center});
      }
    }
    const segments = [];
    for (const [id, members] of mesh ? [] : formations) {
      for (let index = 0; index < members.length; index++) {
        const source = members[index];
        const next = members[index + 1] ?? (source.formation.closed ? members[0] : null);
        if (!next || (next.formation.order !== source.formation.order + 1
                    && !(source.formation.closed && source.formation.order === source.formation.size - 1
                         && next.formation.order === 0))) continue;
        const a = active.get(source.id)?.point, b = active.get(next.id)?.point;
        if (a && b) segments.push({a, b, depth: (a[2] + b[2])/2, hue: hue(id),
                                  dim: selected && selected !== '$' + id
                                        && selected !== source.id && selected !== next.id});
      }
    }
    segments.sort((a,b) => b.depth - a.depth);
    ctx.lineCap = 'round';
    for (const {a,b,depth,hue,dim} of segments) {
      ctx.lineWidth = Math.max(4, Math.min(width/11, extent * .12 * focal / depth));
      line(a, b, `hsl(${hue} 78% 51%)`, dim ? .14 : .6);
      ctx.lineWidth *= .4;
      line(a, b, `hsl(${hue} 92% 80%)`, dim ? .13 : .65);
    }
    ctx.lineWidth = 1;
    for (const source of sources) {
      const local = now - source.start;
      if (local < 0 || local >= source.frames + rate) continue;
      const color = `hsl(${source.hue} 82% 67%)`;
      const dim = !isSelected(source, selected);
      const tailStart = Math.max(0, local - rate);
      const tailEnd = Math.min(local, source.frames - 1);
      let previous = project(position(source.keys, tailStart));
      for (let i = 1; i <= 14 && tailEnd > tailStart; i++) {
        const at = tailStart + (tailEnd - tailStart)*i/14;
        const p = project(position(source.keys, at));
        line(previous, p, color, (dim ? 0.17 : 0.75) * Math.max(0, 1 - (local-at)/rate));
        previous = p;
      }
      if (local < source.frames) {
        const anchor = active.get(source.id);
        const bass = source.bass?.[Math.floor(local / Math.floor(rate / 2))];
        const seconds = local / rate;
        const level = !mesh && source.trace?.length ? envelopeAt(source.wave, seconds) : null;
        if (anchor && (!mesh || !source.wave.length || sourcePowerAt(source, seconds) > 0)) {
          markers.push({...anchor, source, seconds, level, color, radius: dim ? 3 : 5,
                        bass, dim, label: selected === source.id ? source.label : ''});
        }
      }
    }
    markers.sort((a,b) => b.point[2] - a.point[2]);
    const columns = Math.max(28, Math.min(64, Math.floor(1500 / Math.max(1, active.size))));
    for (const marker of markers) {
      const {point, radius, color, label, bass, dim, level} = marker;
      if (level != null && hasRecentSignal(marker.source.trace, marker.seconds)) {
        drawSourceWave(ctx, project, marker, extent, columns);
      } else if (level == null && bass) {
        ctx.globalAlpha = dim ? .35 : 1;
        ctx.strokeStyle = '#ffcc6c';
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(point[0], point[1], radius + 6, 0, Math.PI*2);
        ctx.stroke();
        ctx.globalAlpha = 1;
        ctx.lineWidth = 1;
      }
      if (level == null) dot(point, radius, color);
      ctx.fillStyle = color;
      if (label) ctx.fillText(label, point[0]+10, point[1]+4, width/2);
    }
  }

  function tick() {
    frameId = 0;
    draw();
    if (dialog.open && audio && !audio.paused && !audio.ended) {
      frameId = requestAnimationFrame(tick);
    }
  }
  function sync() {
    play.textContent = audio?.paused || audio?.ended ? '再生' : '一時停止';
    if (audio) volume.value = audio.volume;
    if (!frameId) tick();
  }
  function stopDrawing() {
    if (frameId) cancelAnimationFrame(frameId);
    frameId = 0;
  }
  for (const event of ['play', 'pause', 'ended', 'seeking', 'seeked', 'loadedmetadata', 'volumechange']) {
    on(audio, event, sync);
  }
  on(dialog, 'close', stopDrawing);
  on(play, 'click', () => {
    if (audio.paused) {
      if (audio.ended) audio.currentTime = 0;
      audio.play().catch(sync);
    } else audio.pause();
  });
  on(seek, 'input', () => { if (audio) audio.currentTime = Number(seek.value); });
  on(volume, 'input', () => { if (audio) audio.volume = Number(volume.value); });
  on(select, 'change', draw);
  function syncMode() {
    const field = view.value === 'field';
    fieldControls.hidden = !field;
    canvas.setAttribute('aria-label', field ? '3D空間での直達音の広がりの推定' : '3D空間内の音源ごとの波形');
    draw();
  }
  on(view, 'change', syncMode);
  on(fieldLevel, 'input', () => {
    find('field-value').textContent = `${fieldLevel.value.replace('-', '−')} dB`;
    draw();
  });
  function magnify(amount) {
    zoom = Math.max(.45, Math.min(2.2, zoom*amount));
    draw();
  }
  on(find('closer'), 'click', () => magnify(1.2));
  on(find('farther'), 'click', () => magnify(1/1.2));
  on(find('reset'), 'click', () => { yaw = pitch = .35; zoom = 1; draw(); });
  on(canvas, 'wheel', event => {
    event.preventDefault();
    magnify(Math.exp(-event.deltaY * .001));
  });
  on(canvas, 'pointerdown', event => {
    if (!event.isPrimary || event.button !== 0) return;
    canvas.setPointerCapture(event.pointerId);
    pointer = [event.clientX, event.clientY];
  });
  on(canvas, 'pointermove', event => {
    if (!event.isPrimary || !pointer) return;
    yaw += (event.clientX - pointer[0]) * .009;
    pitch = Math.max(-1.48, Math.min(1.48, pitch + (event.clientY - pointer[1])*.009));
    pointer = [event.clientX, event.clientY];
    draw();
  });
  on(canvas, 'lostpointercapture', () => { pointer = null; });
  const resize = new ResizeObserver(draw);
  resize.observe(canvas);
  rate = data.rate;
  referenceDistance = data.referenceDistance;
  maxDirectGain = data.maxDirectGain;
  fieldCache = undefined;
  sources = data.sources.map(s => ({
    ...s, hue: hue(s.formation?.id ?? s.id),
    wave: s.wave ? Uint8Array.from(atob(s.wave), char => char.charCodeAt(0)) : [],
    trace: s.trace ? Int8Array.from(atob(s.trace), char => char.charCodeAt(0)) : null,
  }));
  let maxBin = 0;
  for (const source of sources) {
    for (const value of source.wave) maxBin = Math.max(maxBin, value);
  }
  referencePower = (maxBin / 255) ** 4;
  view.querySelector('option[value="field"]').disabled = !sources.some(source => source.wave.length);
  if (view.selectedOptions[0]?.disabled) view.value = 'wave';
  formations = new Map();
  for (const source of sources) {
    if (!source.formation) continue;
    const id = source.formation.id;
    if (!formations.has(id)) formations.set(id, []);
    formations.get(id).push(source);
  }
  for (const members of formations.values()) {
    members.sort((a,b) => a.formation.order - b.formation.order);
  }
  find('bass-legend').hidden = !sources.some(s => s.bass?.some(Boolean));
  extent = sources.reduce((max, source) => source.keys.reduce((r, key) => Math.max(r, key[4]), max), 1);
  select.replaceChildren(new Option('すべて', ''),
    ...Array.from(formations, ([id, members]) => new Option(members[0].formation.name, '$' + id)),
    ...sources.map(s => new Option(s.label === s.id ? s.id : `${s.label} · ${s.id}`, s.id)));
  syncMode();
  sync();
  return () => {
    controls.abort();
    resize.disconnect();
    stopDrawing();
  };
}
