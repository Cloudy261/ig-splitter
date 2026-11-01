'use strict';

const $ = (id) => document.getElementById(id);
const RATIOS = { '3:4': [3, 4], '4:3': [4, 3] };  // tile width : height
const MAX_TILES = 20;  // Instagram carousel limit
const FIELDS = [['fx', 'x'], ['fy', 'y'], ['fw', 'w'], ['fh', 'h']];
const SETTINGS = ['n', 'check', 'ratio', 'magnet', 'fill', 'border', 'igSize'];  // remembered per browser
const STORE_KEY = 'ig-splitter-settings';

const state = {
  info: null,         // image_info() of the selected image
  rect: null,         // frame {x, y, w, h} in image pixels, display orientation; may stick out in border mode
  view: null,         // area the canvas shows, in image pixels (the photo, plus room for a border)
  config: {},
  n: 3,
  check: true,        // Instagram ratio check
  ratio: '3:4',
  magnet: true,
  fill: 'crop',       // 'crop' or 'pad' (add a border)
  border: '#ffffff',
  igSize: true,       // export at Instagram size (re-encoded) instead of lossless
  bannerHidden: false,
};
let drag = null;
let hintCache = { key: '', best: null };

const canvas = $('canvas'), frame = $('frame'), shade = $('shade'), pad = $('pad'), preview = $('preview');

// ---------- helpers ----------

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  };
  const res = await fetch(path, options);
  const data = await res.json();
  if (res.status === 401 && data.login) throw needLogin();
  if (!res.ok || data.error) throw new Error(data.error || res.statusText);
  return data;
}

/** Show the login screen. The returned error is one `fail` stays quiet about. */
function needLogin() {
  $('login').hidden = false;
  $('login-password').focus();
  const error = new Error('Please log in');
  error.login = true;
  return error;
}

function el(tag, cls = '', text = '') {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text) node.textContent = text;
  return node;
}

const mod = (a, m) => ((a % m) + m) % m;
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
const fmt = (v) => (Number.isInteger(v) ? String(v) : v.toFixed(2));
const percent = (v) => `${Math.round(v * 100)} %`;
const area = (r) => (r ? r.w * r.h : 0);
const previewUrl = (img, size) => `/api/preview?name=${encodeURIComponent(img.name)}&size=${size}&v=${img.mtime}`;

function intersect(a, b) {
  const x = Math.max(a.x, b.x), y = Math.max(a.y, b.y);
  const w = Math.min(a.x + a.w, b.x + b.w) - x, h = Math.min(a.y + a.h, b.y + b.h) - y;
  return w > 0 && h > 0 ? { x, y, w, h } : null;
}

function ask(text, buttons) {
  const dialog = $('dialog');
  return new Promise((resolve) => {
    $('dialog-text').textContent = text;
    $('dialog-buttons').replaceChildren(...buttons.map(([value, label], i) => {
      const button = el('button', i === 0 ? 'primary' : '', label);
      button.onclick = () => { resolve(value); dialog.close(); };
      return button;
    }));
    dialog.onclose = () => resolve('cancel');  // Esc
    dialog.showModal();
  });
}

const fail = (e) => (e.login ? null : ask(e.message, [['ok', 'OK']]));

function loadSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORE_KEY) || '{}');
    for (const key of SETTINGS) if (key in saved) state[key] = saved[key];
  } catch { /* no storage: keep the defaults */ }
  if (!RATIOS[state.ratio]) state.ratio = '3:4';
  if (!['crop', 'pad'].includes(state.fill)) state.fill = 'crop';
}

function saveSettings() {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(Object.fromEntries(SETTINGS.map((k) => [k, state[k]]))));
  } catch { /* no storage: settings just aren't remembered */ }
}

// ---------- geometry ----------

const imageRect = () => ({ x: 0, y: 0, w: state.info.width, h: state.info.height });

/** Target tile width / height, or null when the ratio check is off. */
function targetRatio() {
  if (!state.check) return null;
  const [a, b] = RATIOS[state.ratio];
  return a / b;
}

const tileHeight = (tw) => Math.round(tw / targetRatio());

/** Where cut lines may go: the JPEG block grid for lossless cuts, any pixel when re-encoding anyway. */
function grid() {
  const free = { step: 1, anchor: 'start' };
  return state.igSize ? { x: free, y: free } : state.info.grid;
}

/** Instagram width of a tile: 1080 (3:4 → 1080 × 1440), 1440 for 4:3 (1440 × 1080). */
const igWidth = () => (state.check && state.ratio === '4:3' ? 1440 : 1080);
/** Instagram export size of a tile; exactly 1080 × 1440 (or 1440 × 1080) when the ratio is checked. */
const exportSize = (tw, th) => [igWidth(), state.check ? tileHeight(igWidth()) : Math.round((th * igWidth()) / tw)];

/** Most tiles this image can be cut into. */
function maxTiles() {
  return clamp(Math.floor(state.info.width / grid().x.step), 2, MAX_TILES);
}

/** Nearest allowed start of a span of `size` px between lo and hi (block grid of an axis of `total` px). */
function snapPos(v, size, total, { step, anchor }, lo, hi) {
  const off = anchor === 'start' ? 0 : mod(total - size, step);
  const first = lo + mod(off - lo, step);
  const last = first + Math.floor((hi - size - first) / step) * step;
  return clamp(first + Math.round((v - first) / step) * step, first, last);
}

/** Keep a frame inside `b` (default: the view); with `snap`, also put it on the block grid. */
function place(r, snap = state.magnet, b = state.view) {
  const { width, height } = state.info, g = grid();
  if (snap) {
    return { ...r, x: snapPos(r.x, r.w, width, g.x, b.x, b.x + b.w), y: snapPos(r.y, r.h, height, g.y, b.y, b.y + b.h) };
  }
  return { ...r, x: clamp(Math.round(r.x), b.x, b.x + b.w - r.w), y: clamp(Math.round(r.y), b.y, b.y + b.h - r.h) };
}

/** Which checks a frame passes. */
function evaluate(r = state.rect) {
  const { n, info } = state, g = grid();
  const tw = r.w / n;
  const divisible = Number.isInteger(tw);
  const aligned = divisible && tw % g.x.step === 0
    && mod(g.x.anchor === 'start' ? r.x : info.width - r.x - r.w, g.x.step) === 0
    && mod(g.y.anchor === 'start' ? r.y : info.height - r.y - r.h, g.y.step) === 0;
  let ratioOk = true;
  if (state.check) {
    const [a, b] = RATIOS[state.ratio];
    ratioOk = Math.abs(b * tw - a * r.h) <= Math.max(a, b) / 2;  // exact up to pixel rounding
  }
  return { tw, divisible, aligned, ratioOk, valid: divisible && aligned && ratioOk };
}

/**
 * Crop: closest valid frame to `r` inside the photo. Keeps its centre and tile height, makes
 * the width split into equal tiles of the target ratio, and shrinks if the photo is too small.
 */
function cropFit(r, n) {
  const { info } = state, { step } = grid().x, ratio = targetRatio();
  let tw = ratio ? Math.round((r.h * ratio) / step) * step : Math.floor(r.w / n / step) * step;
  tw = clamp(tw, step, Math.floor(info.width / n / step) * step);
  if (ratio) while (tw > step && tileHeight(tw) > info.height) tw -= step;
  const w = n * tw, h = ratio ? tileHeight(tw) : r.h;
  return place({ x: r.x + (r.w - w) / 2, y: r.y + (r.h - h) / 2, w, h }, true, imageRect());
}

/**
 * Border: valid frame around `r`. It fits `r` tightly along one axis (trimming a few px
 * rather than adding a hairline border) and adds the border along the other.
 */
function padFit(r, n) {
  const { step } = grid().x, ratio = targetRatio();
  const byWidth = Math.floor(r.w / n / step) * step;
  const byHeight = ratio ? Math.floor((r.h * ratio) / step) * step : 0;
  const tw = Math.max(step, byWidth, byHeight);
  const w = n * tw, h = ratio ? tileHeight(tw) : r.h;
  const room = { x: r.x - w, y: r.y - h, w: r.w + 2 * w, h: r.h + 2 * h };
  return place({ x: r.x + (r.w - w) / 2, y: r.y + (r.h - h) / 2, w, h }, true, room);
}

/** Nearest valid frame for the part of the photo that `r` shows. */
function snapNear(r, n = state.n) {
  const photo = intersect(r, imageRect()) || imageRect();
  return state.fill === 'pad' ? padFit(photo, n) : cropFit(photo, n);
}

const bestFit = (n = state.n) => (state.fill === 'pad' ? padFit(imageRect(), n) : cropFit(imageRect(), n));

/** Frame after dragging `handle` (n, ne, e, ...) by (dx, dy) image px, starting from frame `s`. */
function resizeTo(s, handle, dx, dy) {
  const { n } = state, ratio = targetRatio(), { step } = grid().x, b = state.view;
  const E = handle.includes('e'), W = handle.includes('w'), N = handle.includes('n'), S = handle.includes('s');
  const rw = s.w + (E ? dx : W ? -dx : 0), rh = s.h + (S ? dy : N ? -dy : 0);
  // room left in the view, given the edges that stay put
  const maxW = E ? b.x + b.w - s.x : W ? s.x + s.w - b.x : b.w;
  const maxH = S ? b.y + b.h - s.y : N ? s.y + s.h - b.y : b.h;
  let w, h;
  if (!state.magnet) {
    w = clamp(Math.round(rw), n, maxW);
    h = clamp(Math.round(rh), 1, maxH);
  } else {
    let tw = E || W ? rw / n : s.w / n;
    if (ratio && (N || S)) tw = E || W ? Math.max(tw, rh * ratio) : rh * ratio;
    tw = clamp(Math.round(tw / step) * step, step, Math.max(step, Math.floor(maxW / n / step) * step));
    if (ratio) {
      while (tw > step && tileHeight(tw) > maxH) tw -= step;
      h = tileHeight(tw);
    } else {
      h = clamp(Math.round(rh), 1, maxH);
    }
    w = n * tw;
  }
  // the opposite edge stays put; on a middle handle the frame stays centred
  const x = E ? s.x : W ? s.x + s.w - w : s.x + (s.w - w) / 2;
  const y = S ? s.y : N ? s.y + s.h - h : s.y + (s.h - h) / 2;
  return place({ x, y, w, h });
}

const moveTo = (s, dx, dy) => place({ ...s, x: s.x + dx, y: s.y + dy });

/**
 * Lossless JPEG with a border: a partial last block row/column of the photo can't sit next
 * to the border, so the server leaves it out. Returns those px per side (same rule as splitter.py).
 */
function trims(r = state.rect) {
  const t = { top: 0, right: 0, bottom: 0, left: 0 };
  if (state.igSize || state.fill !== 'pad') return t;
  const { width: W, height: H, grid: g } = state.info;
  if (r.x + r.w > W && g.x.anchor === 'start') t.right = W % g.x.step;
  if (r.x < 0 && g.x.anchor === 'end') t.left = W % g.x.step;
  if (r.y + r.h > H && g.y.anchor === 'start') t.bottom = H % g.y.step;
  if (r.y < 0 && g.y.anchor === 'end') t.top = H % g.y.step;
  return t;
}

/** Canvas area: the photo; in border mode also the best bordered frame and the current one, plus a margin. */
function computeView() {
  const img = imageRect();
  if (state.fill !== 'pad') {
    state.view = img;
    return;
  }
  const boxes = [img, padFit(img, state.n), state.rect];
  const x0 = Math.min(...boxes.map((b) => b.x)), y0 = Math.min(...boxes.map((b) => b.y));
  const x1 = Math.max(...boxes.map((b) => b.x + b.w)), y1 = Math.max(...boxes.map((b) => b.y + b.h));
  const m = Math.round(0.08 * Math.max(x1 - x0, y1 - y0));
  state.view = { x: x0 - m, y: y0 - m, w: x1 - x0 + 2 * m, h: y1 - y0 + 2 * m };
}

/** Tile count with the best fit: full Instagram resolution first, then the most photo, then fewer tiles. */
function bestCount() {
  if (!state.check) return null;
  const key = [state.info.name, state.info.mtime, state.fill, state.ratio, state.igSize].join('|');
  if (hintCache.key === key) return hintCache.best;
  const img = imageRect(), sharpWidth = igWidth();
  let best = null;
  for (let n = 2; n <= maxTiles(); n++) {
    const f = bestFit(n), tw = f.w / n;
    const cover = area(intersect(f, img)) / Math.max(area(f), area(img));
    const sharp = tw >= sharpWidth;
    if (!best || (sharp && !best.sharp) || (sharp === best.sharp && cover > best.cover + 0.005)) {
      best = { n, cover, tw, sharp };
    }
  }
  hintCache = { key, best };
  return best;
}

// ---------- rendering ----------

function layout() {
  if (!state.info) return;
  const v = state.view, stage = $('stage'), padX = 40, padTop = 48, padBottom = 80;
  const availW = stage.clientWidth - 2 * padX, availH = stage.clientHeight - padTop - padBottom;
  const scale = Math.min(availW / v.w, availH / v.h);
  const w = v.w * scale, h = v.h * scale;
  Object.assign(canvas.style, {
    width: `${w}px`, height: `${h}px`,
    left: `${(stage.clientWidth - w) / 2}px`, top: `${padTop + (availH - h) / 2}px`,
  });
}

/** CSS position of an image-pixel rectangle inside the canvas. */
function box(r) {
  const v = state.view, pct = (a, total) => `${(a / total) * 100}%`;
  return { left: pct(r.x - v.x, v.w), top: pct(r.y - v.y, v.h), width: pct(r.w, v.w), height: pct(r.h, v.h) };
}

function buildTiles() {
  $('tiles').replaceChildren(...Array.from({ length: state.n }, (_, i) => {
    const tile = el('div', 'tile');
    tile.append(el('span', '', String(i + 1)));
    return tile;
  }));
}

function problems(ev) {
  const { info, rect: r, n } = state, [bw, bh] = info.block;
  const out = [];
  if (!ev.divisible) out.push(`${r.w} px can't be split evenly into ${n} tiles.`);
  else if (!ev.aligned) out.push(`The cut lines miss the ${bw}×${bh} px JPEG block grid, so the cut wouldn't be lossless.`);
  if (!ev.ratioOk) out.push(`Tiles would be ${fmt(ev.tw)} × ${r.h} px, not ${state.ratio}.`);
  return out.join(' ');
}

function renderChecks(ev) {
  const { info, rect: r, n } = state;
  const items = [];
  const add = (cls, text, note) => {
    const body = el('div', '', text);
    if (note) body.append(el('small', '', note));
    const li = el('li', cls);
    li.append(body);
    items.push(li);
  };
  if (ev.divisible) add('ok', `Splits evenly: ${n} × ${ev.tw} px`);
  else add('bad', `${r.w} px ÷ ${n} = ${fmt(ev.tw)} px`, 'Tiles must be equally wide');

  const [ow, oh] = exportSize(ev.tw, r.h);
  if (state.igSize) {
    if (ev.tw >= ow) add('ok', `Exports at ${ow} × ${oh} px`, `Scaled down from ${fmt(ev.tw)} × ${r.h} px and re-encoded`);
    else add('warn', `Tiles are smaller than ${ow} × ${oh}`, `They stay ${fmt(ev.tw)} × ${r.h} px; Instagram will upscale them (softer)`);
  } else {
    if (info.format === 'JPEG') {
      const blocks = `${info.block[0]}×${info.block[1]} px JPEG block grid`;
      if (ev.aligned) add('ok', 'Lossless JPEG cut', `Cut lines sit on the ${blocks}`);
      else add('bad', 'Not lossless yet', `Cut lines must sit on the ${blocks}`);
    } else {
      add('ok', 'Lossless PNG', info.detail);
    }
    if (ev.tw < igWidth()) add('warn', `Tiles are only ${fmt(ev.tw)} px wide`, `Instagram shows ${ow} × ${oh} and will upscale them`);
  }

  if (state.check) {
    const [a, b] = RATIOS[state.ratio];
    const actual = `${fmt(ev.tw)} × ${r.h} px`;
    if (ev.ratioOk) add('ok', `Tiles are ${state.ratio}`, actual);
    else add('bad', `Tiles aren't ${state.ratio}`, `${actual} = ${(ev.tw / r.h).toFixed(2)} : 1 instead of ${(a / b).toFixed(2)} : 1`);
  }

  if (state.fill === 'pad') {
    const t = trims();
    const sides = {
      top: Math.max(0, -r.y) + t.top, right: Math.max(0, r.x + r.w - info.width) + t.right,
      bottom: Math.max(0, r.y + r.h - info.height) + t.bottom, left: Math.max(0, -r.x) + t.left,
    };
    const list = Object.entries(sides).filter(([, v]) => v > 0).map(([side, v]) => `${v} ${side}`);
    const trimmed = Object.entries(t).filter(([, v]) => v > 0).map(([side, v]) => `${v} px ${side}`);
    add('info', list.length ? `Border: ${list.join(' · ')} px` : 'No border needed',
      trimmed.length ? `Includes the photo's partial JPEG block (${trimmed.join(', ')})` : '');
  }
  const used = area(intersect(r, imageRect()));
  add('info', `Frame ${r.w} × ${r.h} px`, `Uses ${percent(used / (info.width * info.height))} of the photo`);
  $('checks').replaceChildren(...items);
}

function renderBanner(ev) {
  const fix = ev.valid || state.bannerHidden || drag ? null : snapNear(state.rect);
  $('banner').hidden = !fix;
  if (!fix) return;
  $('banner-text').textContent = `${problems(ev)} Snap to the nearest valid size?`;
  $('banner-fix').textContent = `${state.fill === 'pad' ? 'Pad' : 'Crop'} to ${fix.w} × ${fix.h}`;
}

function renderHint() {
  const hint = $('hint'), best = bestCount();
  hint.hidden = !best;
  if (!best) return;
  const what = state.fill === 'pad' ? `${percent(best.cover)} photo, rest border` : `keeps ${percent(best.cover)} of the photo`;
  const small = best.sharp ? '' : ` · only ${Math.round(best.tw)} px wide`;
  if (best.n === state.n) {
    hint.replaceChildren(el('div', '', `✓ Best fit for ${state.ratio}: ${what}${small}`));
    return;
  }
  const button = el('button');
  button.append('Best fit: ', el('b', '', `${best.n} tiles`), ` · ${what}${small}`);
  button.onclick = () => { setN(best.n); rulesChanged(); };
  hint.replaceChildren(button);
}

function render() {
  const { info, rect: r, n } = state;
  $('ig-size-label').textContent = state.check
    ? `Instagram size (${igWidth()} × ${tileHeight(igWidth())})` : 'Instagram size (1080 px wide)';
  $('ig-size-note').textContent = state.igSize
    ? 'Tiles are scaled down and re-encoded (JPEG quality 95).'
    : 'Lossless: original pixels, nothing re-encoded.';
  if (!info) return;
  const ev = evaluate();
  Object.assign(preview.style, box(imageRect()));
  for (const node of [frame, shade, pad]) Object.assign(node.style, box(r));
  pad.hidden = state.fill !== 'pad';
  pad.style.background = state.border;
  const t = trims(), pct = (a, total) => `${(a / total) * 100}%`;
  preview.style.clipPath = `inset(${pct(t.top, info.height)} ${pct(t.right, info.width)} ${pct(t.bottom, info.height)} ${pct(t.left, info.width)})`;
  frame.classList.toggle('invalid', !ev.valid);
  $('frame-label').textContent = `${n} × ${fmt(ev.tw)} × ${r.h} px`;
  for (const [id, key] of FIELDS) if (document.activeElement !== $(id)) $(id).value = r[key];
  renderChecks(ev);
  renderBanner(ev);
  renderHint();
  $('export').textContent = `Export ${n} tiles`;
  $('export').disabled = false;
  $('preview-carousel').disabled = false;
}

/** Put the controls in line with `state`. */
function syncControls() {
  $('n').value = state.n;
  $('check').checked = state.check;
  $('ratio').classList.toggle('off', !state.check);
  for (const b of $('ratio').children) b.classList.toggle('on', b.dataset.ratio === state.ratio);
  $('magnet').checked = state.magnet;
  for (const b of $('fill').children) b.classList.toggle('on', b.dataset.fill === state.fill);
  $('border-row').hidden = state.fill !== 'pad';
  for (const s of document.querySelectorAll('.swatch')) s.classList.toggle('on', s.dataset.color === state.border);
  $('border-color').value = state.border;
  $('ig-size').checked = state.igSize;
}

// ---------- actions ----------

function setN(v) {
  state.n = clamp(Math.round(v) || 2, 2, state.info ? maxTiles() : MAX_TILES);
  $('n').value = state.n;
  buildTiles();
}

function setRect(r) {
  state.rect = r;
  computeView();
  layout();
  render();
}

/**
 * Tile count, ratio, check or export mode changed: the magnet snaps (a new tile shape starts
 * from the best fit), otherwise the banner asks.
 */
function rulesChanged(newShape = false) {
  saveSettings();
  syncControls();
  state.bannerHidden = false;
  if (!state.info) return render();
  if (state.magnet) state.rect = newShape ? bestFit() : snapNear(state.rect);
  setRect(state.rect);
}

function applyConfig(cfg) {
  state.config = cfg;
  for (const [id, key] of [['input-dir', 'input_dir'], ['output-dir', 'output_dir']]) {
    $(id).value = cfg[key];
    $(id).readOnly = cfg.locked;
  }
  $('save-folders').hidden = cfg.locked;
  for (const b of document.querySelectorAll('.browse')) b.hidden = !cfg.can_pick;
  if (cfg.locked) $('folders-msg').textContent = 'Set by the server (Docker).';
}

async function loadGallery() {
  const { dir, images, missing } = await api('/api/images');
  $('input-path').textContent = `\u200E${dir}\u200E`;  // LRMs: the label is rtl, keep a leading "/" in front
  $('input-path').title = dir;
  const list = $('gallery');
  if (missing) return list.replaceChildren(el('li', 'hint', 'Input folder not found. Change it under Folders.'));
  if (!images.length) return list.replaceChildren(el('li', 'hint', 'No photos yet. Drop some here or use “Add photos”.'));
  list.replaceChildren(...images.map((img) => {
    const li = el('li', state.info?.name === img.name ? 'on' : '');
    li.dataset.name = img.name;
    const thumb = el('img');
    thumb.loading = 'lazy';
    thumb.src = previewUrl(img, 400);
    const text = el('div');
    text.append(el('b', '', img.name), el('small', '', `${img.width} × ${img.height} · ${img.format}`));
    li.append(thumb, text);
    li.onclick = () => selectImage(img.name).catch(fail);
    return li;
  }));
}

async function selectImage(name) {
  const info = await api(`/api/image?name=${encodeURIComponent(name)}`);
  state.info = info;
  state.rect = imageRect();  // not snapped: the banner asks first
  state.bannerHidden = false;
  setN(state.n);
  preview.src = previewUrl(info, 3000);
  $('empty').hidden = true;
  canvas.hidden = false;
  $('image-meta').replaceChildren(el('b', '', info.name), `${info.width} × ${info.height} px · ${info.detail}`);
  $('result').replaceChildren();
  for (const li of $('gallery').children) li.classList.toggle('on', li.dataset.name === name);
  setRect(state.rect);
}

async function upload(files) {
  const photos = files.filter((f) => /\.(jpe?g|png)$/i.test(f.name));
  if (!photos.length) throw new Error('Only JPEG and PNG files can be added.');
  const button = $('add-photos');
  button.disabled = true;
  const names = [];
  try {
    for (const [i, file] of photos.entries()) {
      button.textContent = `Uploading ${i + 1} of ${photos.length}…`;
      const res = await fetch('/api/upload', {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream', 'X-Filename': encodeURIComponent(file.name) },
        body: file,
      });
      const data = await res.json();
      if (res.status === 401 && data.login) throw needLogin();
      if (!res.ok || data.error) throw new Error(data.error || res.statusText);
      names.push(data.name);
    }
  } finally {
    button.disabled = false;
    button.textContent = '+ Add photos';
    await loadGallery();
  }
  await selectImage(names[0]);
}

async function exportTiles() {
  let ev = evaluate();
  const verb = state.fill === 'pad' ? 'Pad' : 'Crop';
  if (!ev.valid) {
    const fix = snapNear(state.rect);
    const hard = !ev.divisible || !ev.aligned;
    const choice = hard
      ? await ask(problems(ev), [['fix', `${verb} to ${fix.w} × ${fix.h} & export`], ['cancel', 'Cancel']])
      : await ask(`${problems(ev)} Instagram would crop or pad them.`,
        [['fix', `${verb} to ${state.ratio} & export`], ['anyway', 'Export anyway'], ['cancel', 'Cancel']]);
    if (choice === 'cancel') return;
    if (choice === 'fix') {
      setRect(fix);
      ev = evaluate();
    }
  }
  const { info, rect: r, n } = state;
  const [ow, oh] = exportSize(ev.tw, r.h);
  if (state.igSize && ev.tw < ow) {
    const choice = await ask(
      `The tiles are ${ev.tw} × ${r.h} px, smaller than Instagram's ${ow} × ${oh}. They'll be exported at their own size and `
      + 'Instagram will upscale them, which looks softer. Fewer tiles make them bigger.',
      [['go', 'Export anyway'], ['cancel', 'Cancel']]);
    if (choice !== 'go') return;
  }
  const box = $('result');
  $('export').disabled = true;
  box.replaceChildren(el('div', '', 'Exporting…'));
  try {
    const { dir, files } = await api('/api/split', {
      name: info.name, rect: [r.x, r.y, r.w, r.h], tiles: n,
      border: state.fill === 'pad' ? state.border : null,
      size: state.igSize ? [ow, oh] : null,
    });
    const query = (f) => `name=${encodeURIComponent(info.name)}&file=${encodeURIComponent(f)}`;
    const list = el('ol');
    for (const f of files) {
      const link = el('a', '', f);
      link.href = `/api/download?${query(f)}`;
      link.download = f;
      const li = el('li');
      li.append(link);
      list.append(li);
    }
    const buttons = el('div', 'row');
    const zip = el('button', '', 'Download all (.zip)');
    zip.onclick = () => { location.href = `/api/zip?name=${encodeURIComponent(info.name)}&${files.map((f) => `file=${encodeURIComponent(f)}`).join('&')}`; };
    buttons.append(zip);
    if (state.config.can_reveal) {
      const reveal = el('button', '', 'Show in Finder');
      reveal.onclick = () => api('/api/reveal', { name: info.name }).catch(fail);
      buttons.append(reveal);
    }
    box.replaceChildren(el('div', 'ok', `✓ ${files.length} tiles saved to ${dir}`), list, buttons);
  } catch (e) {
    box.replaceChildren(el('div', 'bad', e.message));
  } finally {
    $('export').disabled = false;
  }
}

// ---------- swipe preview ----------

const slides = $('slides');
const currentSlide = () => Math.round(slides.scrollLeft / slides.clientWidth);

function updateSlide() {
  const i = currentSlide();
  $('slide-count').textContent = `${i + 1}/${state.n}`;
  [...$('dots').children].forEach((dot, j) => dot.classList.toggle('on', j === i));
}

function goSlide(step) {
  slides.scrollTo({ left: clamp(currentSlide() + step, 0, state.n - 1) * slides.clientWidth, behavior: 'smooth' });
}

function openCarousel() {
  const { info, rect: r, n } = state, tw = r.w / n;
  const width = Math.round(Math.min(340, (500 * tw) / r.h, window.innerWidth - 80));
  const k = width / tw;  // preview px per image px
  document.querySelector('.phone').style.width = `${width}px`;
  slides.replaceChildren(...Array.from({ length: n }, (_, i) => {
    const slide = el('div', 'slide');
    Object.assign(slide.style, {
      height: `${Math.round(r.h * k)}px`,
      backgroundColor: state.fill === 'pad' ? state.border : '#000',
      backgroundImage: `url("${preview.src}")`,
      backgroundSize: `${info.width * k}px ${info.height * k}px`,
      backgroundPosition: `${-(r.x + i * tw) * k}px ${-r.y * k}px`,
    });
    return slide;
  }));
  $('dots').replaceChildren(...Array.from({ length: n }, () => el('span')));
  $('carousel').showModal();
  slides.scrollLeft = 0;
  updateSlide();
}

let swipe = null;  // mouse drag on the slides (touch scrolls natively)
slides.addEventListener('pointerdown', (e) => {
  if (e.pointerType !== 'mouse') return;
  swipe = { x: e.clientX, left: slides.scrollLeft, start: currentSlide() };
  slides.style.scrollSnapType = 'none';
  slides.setPointerCapture(e.pointerId);
});
slides.addEventListener('pointermove', (e) => {
  if (swipe) slides.scrollLeft = swipe.left - (e.clientX - swipe.x);
});
slides.addEventListener('pointerup', (e) => {
  if (!swipe) return;
  const dx = e.clientX - swipe.x;
  const target = clamp(swipe.start + (dx < -40 ? 1 : dx > 40 ? -1 : 0), 0, state.n - 1);
  swipe = null;
  slides.scrollTo({ left: target * slides.clientWidth, behavior: 'smooth' });
  setTimeout(() => { slides.style.scrollSnapType = ''; }, 450);
});
slides.addEventListener('scroll', updateSlide);
$('slide-prev').onclick = () => goSlide(-1);
$('slide-next').onclick = () => goSlide(1);
$('carousel-close').onclick = () => $('carousel').close();
$('carousel').addEventListener('keydown', (e) => {
  if (e.key === 'ArrowLeft') goSlide(-1);
  if (e.key === 'ArrowRight') goSlide(1);
});
$('preview-carousel').onclick = openCarousel;

// ---------- wiring ----------

frame.addEventListener('pointerdown', (e) => {
  if (e.button !== 0) return;
  e.preventDefault();
  drag = { handle: e.target.dataset.h || 'move', start: { ...state.rect }, x: e.clientX, y: e.clientY };
  e.target.setPointerCapture(e.pointerId);
});
frame.addEventListener('pointermove', (e) => {
  if (!drag) return;
  const k = state.view.w / canvas.clientWidth;  // image px per screen px
  const dx = (e.clientX - drag.x) * k, dy = (e.clientY - drag.y) * k;
  state.rect = drag.handle === 'move' ? moveTo(drag.start, dx, dy) : resizeTo(drag.start, drag.handle, dx, dy);
  render();
});
const endDrag = () => {
  drag = null;
  render();
};
frame.addEventListener('pointerup', endDrag);
frame.addEventListener('pointercancel', endDrag);

for (const [id, key] of FIELDS) {
  $(id).addEventListener('change', () => {
    const v = parseInt($(id).value, 10), s = state.rect;
    if (state.info && Number.isFinite(v)) {
      const d = v - s[key];
      if (key === 'x') state.rect = moveTo(s, d, 0);
      else if (key === 'y') state.rect = moveTo(s, 0, d);
      else state.rect = resizeTo(s, key === 'w' ? 'e' : 's', key === 'w' ? d : 0, key === 'h' ? d : 0);
    }
    render();
  });
}

$('n').addEventListener('change', () => { setN(parseInt($('n').value, 10)); rulesChanged(); });
$('n-minus').onclick = () => { setN(state.n - 1); rulesChanged(); };
$('n-plus').onclick = () => { setN(state.n + 1); rulesChanged(); };

$('check').addEventListener('change', (e) => { state.check = e.target.checked; rulesChanged(true); });
for (const button of $('ratio').children) {
  button.onclick = () => { state.ratio = button.dataset.ratio; rulesChanged(true); };
}
$('magnet').addEventListener('change', (e) => {
  state.magnet = e.target.checked;
  if (state.magnet) return rulesChanged();
  saveSettings();
  render();
});
$('ig-size').addEventListener('change', (e) => { state.igSize = e.target.checked; rulesChanged(); });

for (const button of $('fill').children) {
  button.onclick = () => {
    state.fill = button.dataset.fill;
    saveSettings();
    syncControls();
    if (!state.info) return;
    state.rect = imageRect();
    computeView();
    setRect(bestFit());
  };
}
const setBorder = (color) => {
  state.border = color;
  saveSettings();
  syncControls();
  render();
};
for (const swatch of document.querySelectorAll('.swatch')) swatch.onclick = () => setBorder(swatch.dataset.color);
$('border-color').addEventListener('input', (e) => setBorder(e.target.value));

$('best-fit').onclick = () => { if (state.info) setRect(bestFit()); };
$('full').onclick = () => {
  if (!state.info) return;
  state.bannerHidden = false;
  setRect(imageRect());
};
$('banner-fix').onclick = () => setRect(snapNear(state.rect));
$('banner-dismiss').onclick = () => { state.bannerHidden = true; render(); };
$('export').onclick = () => exportTiles().catch(fail);
$('refresh').onclick = () => loadGallery().catch(fail);

$('add-photos').onclick = () => $('file-input').click();
$('file-input').addEventListener('change', (e) => {
  upload([...e.target.files]).catch(fail);
  e.target.value = '';
});
let dragDepth = 0;  // drag & drop of files anywhere onto the window
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
window.addEventListener('dragenter', (e) => {
  if (!hasFiles(e)) return;
  dragDepth += 1;
  $('drop-overlay').hidden = false;
});
window.addEventListener('dragleave', (e) => {
  if (!hasFiles(e)) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) $('drop-overlay').hidden = true;
});
window.addEventListener('dragover', (e) => { if (hasFiles(e)) e.preventDefault(); });
window.addEventListener('drop', (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  dragDepth = 0;
  $('drop-overlay').hidden = true;
  upload([...e.dataTransfer.files]).catch(fail);
});

$('save-folders').onclick = async () => {
  const msg = $('folders-msg');
  try {
    applyConfig(await api('/api/config', { input_dir: $('input-dir').value, output_dir: $('output-dir').value }));
    msg.className = 'msg ok';
    msg.textContent = 'Saved';
    await loadGallery();
  } catch (e) {
    msg.className = 'msg bad';
    msg.textContent = e.message;
  }
};
for (const button of document.querySelectorAll('.browse')) {
  button.onclick = async () => {
    const msg = $('folders-msg');
    msg.className = 'msg';
    msg.textContent = 'Choose a folder in the Finder window…';
    try {
      const cfg = await api('/api/pick-folder', { key: button.dataset.key });
      applyConfig(cfg);
      msg.className = cfg.cancelled ? 'msg' : 'msg ok';
      msg.textContent = cfg.cancelled ? '' : 'Saved';
      if (!cfg.cancelled) await loadGallery();
    } catch (e) {
      msg.className = 'msg bad';
      msg.textContent = e.message;
    }
  };
}

new ResizeObserver(layout).observe($('stage'));
loadSettings();
syncControls();
buildTiles();
$('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('login-msg').textContent = '';
  try {
    await api('/api/login', { password: $('login-password').value });
    $('login-password').value = '';
    $('login').hidden = true;
    await start();
  } catch (err) {
    $('login-msg').textContent = err.message;
  }
});
$('logout').onclick = async () => {
  await api('/api/logout', {}).catch(() => {});
  location.reload();
};

async function start() {
  applyConfig(await api('/api/config'));
  await loadGallery();
}

render();
api('/api/session').then((session) => {
  $('logout').hidden = !session.auth;
  if (session.auth && !session.logged_in) throw needLogin();
  return start();
}).catch(fail);
