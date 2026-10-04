'use strict';
// Public live page, EXPERIMENT_DASHBOARD_V3 (package public-page-v3). Renders the JSON document
// with DOM calls only (textContent; never innerHTML) and polls /api/public/experiment every 30
// seconds, holding the previous render (dimmed) while it reads. Charts are inline SVG drawn
// here: thin marks, one axis per chart, a crosshair or per-mark tooltip on every chart, and a
// "View as table" twin. Two views share this script: the live page and a trade's own page
// (/trade/{n}), whose candles come from this service's own chart route. This origin only; no
// third-party code.
(function () {
  const POLL_MS = 30000;
  const POLL_TIMEOUT_MS = 20000;  // A read still unanswered is dropped; the next poll asks again.
  const DIM_AFTER_MS = 300;  // A read slower than this dims the previous render (no layout jump).
  const CHART_REFRESH_MS = 60000;
  const TZ = 'America/New_York';  // Every time on the page is New York time, marked ET.
  const US = 'en-US';
  const MINUS = '−';
  const DASH = '—';
  const SVG = 'http://www.w3.org/2000/svg';
  const DECISIONS_SHOWN = 8;
  const HOUR = 3600000;
  const DAY = 24 * HOUR;
  const RANGES = [['1D', DAY], ['7D', 7 * DAY], ['30D', 30 * DAY], ['All', null]];
  // Series and status colours (validated 2026-10-03; text never wears them).
  const C = {account: '#1D4ED8', btc: '#C2410C', gain: '#0B7A47', loss: '#B42318',
    ink: '#111418', muted: '#5B6470', grid: '#EEF0F2', axis: '#C9CED4', warn: '#9A5B00',
    jev: '#6B21A8', research: '#8B949E', after: '#9AA3AD', volume: '#C3C9D0'};
  const WHO = {  // Timeline actors: label and colour class.
    RESEARCH_AGENT: ['Agent', 'who-agent'], JEV: ['Jev', 'who-jev'], APP: ['App', 'who-app'],
    TRADE: ['App', 'who-app'], PACING: ['Pacing', 'who-wait'], LIMIT: ['Daily limit',
      'who-limit'],
  };
  let doc = null;
  let receivedAt = Date.now();
  let failures = 0;
  let polling = false;  // One read at a time: a slow answer never stacks up requests.
  const drawn = new Map();  // Section -> the data it was last drawn from (unchanged: kept).
  const ui = {range: '7D', tradeRange: 'TRADE', width: 0};
  const chart = {data: null, at: 0, loading: false, trade: null};

  const byId = (id) => document.getElementById(id);
  const view = () => document.body.dataset.view || 'main';

  function el(tag, cls, words) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (words !== undefined && words !== null) node.textContent = String(words);
    return node;
  }

  function add(parent, ...children) {
    for (const child of children) {
      if (child === null || child === undefined || child === false) continue;
      parent.append(typeof child === 'string' ? document.createTextNode(child) : child);
    }
    return parent;
  }

  function put(parent, ...children) {  // replaceChildren, skipping absent parts.
    parent.replaceChildren();
    return add(parent, ...children);
  }

  function n(words, cls) {  // A figure in the monospace face.
    return el('span', 'n' + (cls ? ' ' + cls : ''), words);
  }

  function s(tag, attrs, words) {  // An SVG element.
    const node = document.createElementNS(SVG, tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value !== null && value !== undefined) node.setAttribute(key, String(value));
    }
    if (words !== undefined && words !== null) node.textContent = String(words);
    return node;
  }

  // --- Numbers ------------------------------------------------------------------------------

  function num(value) {
    if (value === null || value === undefined || value === '') return null;
    const x = Number(value);
    return Number.isFinite(x) ? x : null;
  }

  function money(value, signed, places) {
    const x = num(value);
    if (x === null) return DASH;
    const p = places === undefined ? 2 : places;
    const abs = Math.abs(x).toLocaleString(US, {minimumFractionDigits: p,
      maximumFractionDigits: p});
    if (!signed) return (x < 0 ? MINUS : '') + '$' + abs;
    if (Math.abs(x) < Math.pow(10, -p) / 2) return '$' + (0).toFixed(p);
    return (x > 0 ? '+' : MINUS) + '$' + abs;
  }

  function wholeMoney(value) {
    const x = num(value);
    return x === null ? DASH : (x < 0 ? MINUS : '') + '$' +
      Math.round(Math.abs(x)).toLocaleString(US);
  }

  function rText(value, places) {
    const x = num(value);
    if (x === null) return DASH;
    const p = places === undefined ? 2 : places;
    if (Math.abs(x) < Math.pow(10, -p) / 2) return (0).toFixed(p) + 'R';
    return (x > 0 ? '+' : MINUS) + Math.abs(x).toFixed(p) + 'R';
  }

  function signedPct(value, places) {  // value is already a percent figure.
    const x = num(value);
    if (x === null) return DASH;
    const p = places === undefined ? 2 : places;
    if (Math.abs(x) < Math.pow(10, -p) / 2) return (0).toFixed(p) + '%';
    return (x > 0 ? '+' : MINUS) + Math.abs(x).toFixed(p) + '%';
  }

  function plainPct(fraction, places) {  // 0.275 -> "27.5%"
    const x = num(fraction);
    return x === null ? DASH : (x * 100).toFixed(places === undefined ? 1 : places) + '%';
  }

  function tone(value) {
    const x = num(value);
    if (x === null || Math.abs(x) < 0.005) return '';
    return x > 0 ? 'pos' : 'neg';
  }

  const SUB = '₀₁₂₃₄₅₆₇₈₉';

  // A coin price for people: "64,250.5", "1.8954", and below 0.001 the zeros after the point
  // counted in a subscript, as trading terminals write them: 0.0000042344 -> "0.0₅42344".
  function price(value, digits) {
    const x = num(value);
    if (x === null) return DASH;
    const sig = digits || 5;
    if (x === 0) return '0';
    const a = Math.abs(x);
    const sign = x < 0 ? MINUS : '';
    if (a >= 1000) return sign + a.toLocaleString(US, {maximumFractionDigits: 2});
    if (a >= 1) return sign + a.toLocaleString(US, {maximumFractionDigits: 4});
    if (a >= 0.001) return sign + a.toLocaleString(US, {maximumSignificantDigits: sig});
    const zeros = Math.floor(-Math.log10(a));  // 0.0000042 -> 5 zeros after the point.
    const digitsText = Math.round(a * Math.pow(10, zeros + sig)).toString()
      .slice(0, sig).replace(/0+$/, '') || '0';
    const count = String(zeros).split('').map((d) => SUB[Number(d)]).join('');
    return sign + '0.0' + count + digitsText;
  }

  function priceTick(value, step) {  // An axis price with the step's own precision.
    let places = Math.max(0, Math.ceil(-Math.log10(step) - 1e-9));
    if (Math.abs(step * Math.pow(10, places) - Math.round(step * Math.pow(10, places))) > 1e-6) {
      places += 1;
    }
    if (value > 0 && value < 0.001) {  // Subscript zeros, digits to the step's precision.
      const zeros = Math.floor(-Math.log10(value));
      const digits = String(Math.round(value * Math.pow(10, places)));
      return '0.0' + String(zeros).split('').map((d) => SUB[Number(d)]).join('') + digits;
    }
    return value.toLocaleString(US, {minimumFractionDigits: places,
      maximumFractionDigits: places});
  }

  function quantity(value) {
    const x = num(value);
    if (x === null) return DASH;
    if (x >= 1e6) return x.toLocaleString(US, {maximumFractionDigits: 0});
    if (x >= 1000) return x.toLocaleString(US, {maximumFractionDigits: 2});
    return x.toLocaleString(US, {maximumSignificantDigits: 6});
  }

  function base(symbol) {
    return String(symbol || '?').split('/')[0];
  }

  function plural(count, one, many) {
    return count + ' ' + (count === 1 ? one : many);
  }

  // --- Time (New York) ------------------------------------------------------------------------

  function serverNow() {
    return Date.parse(doc.served_at || doc.as_of) + (Date.now() - receivedAt);
  }

  function etTime(value, seconds) {  // "15:24" / "15:24:05"
    return new Date(value).toLocaleTimeString(US, {hour: '2-digit', minute: '2-digit',
      second: seconds ? '2-digit' : undefined, hourCycle: 'h23', timeZone: TZ});
  }

  function etDayKey(value) {
    return new Date(value).toLocaleDateString('en-CA', {timeZone: TZ});
  }

  function etDay(value, weekday) {  // "Fri Oct 2" / "Oct 2"
    return new Date(value).toLocaleDateString(US, {weekday: weekday ? 'short' : undefined,
      month: 'short', day: 'numeric', timeZone: TZ}).replace(',', '');
  }

  function dayLabel(isoDay, weekday) {  // A calendar day (YYYY-MM-DD), as written.
    return new Date(isoDay.slice(0, 10) + 'T12:00:00Z').toLocaleDateString(US, {
      weekday: weekday ? 'short' : undefined, month: 'short', day: 'numeric', timeZone: 'UTC'})
      .replace(',', '');
  }

  function etStamp(value) {  // "15:24" today, else "Oct 1 15:24"
    if (!value) return DASH;
    const today = etDayKey(serverNow()) === etDayKey(value);
    return today ? etTime(value) : etDay(value) + ' ' + etTime(value);
  }

  function span(fromIso, toIso) {  // "14:43–15:00"
    if (!fromIso) return etStamp(toIso);
    if (!toIso || etTime(fromIso) === etTime(toIso)) return etStamp(fromIso);
    return etStamp(fromIso) + '–' + etTime(toIso);
  }

  function minutesWords(minutes) {
    if (minutes === null || minutes === undefined || minutes < 0) return DASH;
    if (minutes < 60) return plural(minutes, 'minute', 'minutes');
    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    if (hours < 48) return hours + ' h' + (rest ? ' ' + rest + ' min' : '');
    return Math.floor(hours / 24) + ' d ' + (hours % 24) + ' h';
  }

  function ago(iso) {
    const seconds = Math.max(0, (serverNow() - Date.parse(iso)) / 1000);
    if (seconds < 60) return Math.round(seconds) + ' s ago';
    if (seconds < 3600) return Math.floor(seconds / 60) + ' min ago';
    return Math.floor(seconds / 3600) + ' h ago';
  }

  function etMidnight(ms) {  // The New York midnight at or before ms.
    const day = etDayKey(ms);
    let guess = Date.parse(day + 'T05:00:00Z');  // 00:00 EST; 01:00 EDT.
    while (etDayKey(guess) !== day || etTime(guess) !== '00:00') {
      guess -= HOUR;
      if (etDayKey(guess) !== day) return guess + HOUR;
    }
    return guess;
  }

  // --- Small pieces -------------------------------------------------------------------------

  function empty(message) {
    return el('div', 'empty-box', message);
  }

  function pending(words) {
    return el('span', 'pending', words || 'pending');
  }

  function label(words) {
    return el('h2', 'label', words);
  }

  function table(columns, minWidth, cls) {  // columns: [title, align]
    const wrap = el('div', 'table-wrap');
    wrap.tabIndex = 0;
    wrap.setAttribute('role', 'region');
    const t = el('table', cls);
    if (minWidth) t.style.minWidth = minWidth + 'px';
    const tr = el('tr');
    for (const [title, align] of columns) {
      const th = el('th', align === 'r' ? 'r' : '', title);
      th.scope = 'col';
      tr.append(th);
    }
    t.append(add(el('thead'), tr), el('tbody'));
    wrap.append(t);
    return {wrap, body: t.tBodies[0]};
  }

  function td(content, cls, colspan) {
    const cell = el('td', cls);
    if (colspan) cell.colSpan = colspan;
    if (content instanceof Node) cell.append(content);
    else cell.textContent = content === null || content === undefined ? DASH : String(content);
    return cell;
  }

  function twin(words, build) {  // "View as table": the chart's table-view twin, on demand.
    const box = el('details', 'twin');
    const summary = el('summary', null, words || 'View as table');
    box.append(summary);
    let built = false;
    box.addEventListener('toggle', () => {
      if (box.open && !built) {
        built = true;
        box.append(build());
      }
    });
    return box;
  }

  function lineKey(color) {
    const key = el('span', 'line-key');
    key.style.background = color;
    return key;
  }

  function tradeLink(t) {
    const a = el('a', 'coin', base(t.symbol));
    a.href = '/trade/' + t.trade_no;
    return a;
  }

  function armWords(t) {
    return t.tag === 'Jev-managed' ? 'Jev' : t.tag || DASH;
  }

  // --- Chart scaffolding ------------------------------------------------------------------------

  function scale(d0, d1, r0, r1) {
    const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0);
    const f = (v) => r0 + (v - d0) * k;
    f.invert = (p) => (k === 0 ? d0 : d0 + (p - r0) / k);
    return f;
  }

  function niceStep(span, count) {
    const raw = span / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    return (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
  }

  function niceTicks(lo, hi, count) {
    if (!(hi > lo)) {
      const pad = Math.abs(lo) * 0.01 || 1;
      lo -= pad;
      hi += pad;
    }
    const step = niceStep(hi - lo, count);
    const start = Math.floor(lo / step) * step;
    const end = Math.ceil(hi / step) * step;
    const ticks = [];
    for (let v = start; v <= end + step / 2; v += step) ticks.push(Number(v.toPrecision(12)));
    return {ticks, step, lo: start, hi: end};
  }

  const TIME_STEPS = [5 * 60000, 15 * 60000, 30 * 60000, HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR,
    12 * HOUR, DAY, 2 * DAY, 7 * DAY, 14 * DAY, 30 * DAY];

  function timeTicks(t0, t1, width) {  // New York-aligned ticks about every 110 px.
    const want = Math.max(3, Math.floor(width / 84));
    const slack = want <= 4 ? 1 : 0;  // A phone takes one more tick before a longer step.
    const step = TIME_STEPS.find((st) => (t1 - t0) / st <= want + slack) || TIME_STEPS.at(-1);
    const ticks = [];
    if (step >= DAY) {
      let t = etMidnight(t0);
      if (t < t0) t = etMidnight(t + DAY + 2 * HOUR);
      const days = Math.round(step / DAY);
      let i = 0;
      while (t <= t1) {
        if (i % days === 0) ticks.push(t);
        t = etMidnight(t + DAY + 2 * HOUR);
        i += 1;
      }
    } else {
      // Offset of New York from UTC at t0, so hour ticks fall on New York hours.
      const offset = (Date.parse(etDayKey(t0) + 'T' + etTime(t0) + ':00Z') -
        Math.floor(t0 / 60000) * 60000);
      let t = Math.ceil((t0 + offset) / step) * step - offset;
      while (t <= t1) {
        ticks.push(t);
        t += step;
      }
    }
    return {ticks, step};
  }

  function timeLabel(t, step) {
    if (step >= DAY || etTime(t) === '00:00') return etDay(t);
    return etTime(t);
  }

  function frame(host, height, margin) {
    const width = Math.max(260, Math.floor(host.clientWidth || host.getBoundingClientRect()
      .width || 600));
    const svg = s('svg', {width, height, viewBox: '0 0 ' + width + ' ' + height,
      class: 'chart-svg'});
    const plot = {left: margin.left, right: width - margin.right, top: margin.top,
      bottom: height - margin.bottom};
    return {svg, width, height, plot};
  }

  function yAxis(svg, plot, y, ticks, format, side) {
    const g = s('g', {class: 'axis'});
    for (const v of ticks) {
      const py = Math.round(y(v)) + 0.5;
      g.append(s('line', {x1: plot.left, x2: plot.right, y1: py, y2: py, stroke: C.grid}));
      const x = side === 'right' ? plot.right + 8 : plot.left - 8;
      g.append(s('text', {x, y: py + 4, 'text-anchor': side === 'right' ? 'start' : 'end',
        class: 'tick'}, format(v)));
    }
    svg.append(g);
  }

  function xAxis(svg, plot, x, ticks, step) {
    const g = s('g', {class: 'axis'});
    g.append(s('line', {x1: plot.left, x2: plot.right, y1: plot.bottom + 0.5,
      y2: plot.bottom + 0.5, stroke: C.axis}));
    let last = -Infinity;
    for (const t of ticks) {
      const px = Math.round(x(t)) + 0.5;
      if (px < plot.left - 1 || px > plot.right + 1) continue;
      g.append(s('line', {x1: px, x2: px, y1: plot.bottom, y2: plot.bottom + 4, stroke: C.axis}));
      const words = timeLabel(t, step);
      if (px - last < words.length * 7 + 10) continue;  // Never two labels on top of each other.
      g.append(s('text', {x: px, y: plot.bottom + 18, 'text-anchor': 'middle', class: 'tick'},
        words));
      last = px;
    }
    svg.append(g);
  }

  function tooltip(host) {
    const box = el('div', 'tip');
    box.setAttribute('role', 'status');
    box.hidden = true;
    host.append(box);
    return {
      show(px, py, rows) {
        put(box, ...rows);
        box.hidden = false;
        const w = box.offsetWidth, h = box.offsetHeight;
        const hostW = host.clientWidth;
        let left = px + 14;
        if (left + w > hostW) left = px - w - 14;
        box.style.left = Math.max(0, left) + 'px';
        box.style.top = Math.max(0, py - h / 2) + 'px';
      },
      hide() {
        box.hidden = true;
      },
    };
  }

  function tipRow(value, words, color, cls) {  // Values lead, labels follow; a line key.
    return add(el('div', 'tip-row'), color ? lineKey(color) : null,
      n(value, 'tip-value ' + (cls || '')), words ? el('span', 'tip-label', words) : null);
  }

  function nearest(list, t) {  // Index of the item whose .t is nearest t (sorted list).
    let lo = 0, hi = list.length - 1;
    if (hi < 0) return -1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (list[mid].t < t) lo = mid;
      else hi = mid;
    }
    return Math.abs(list[lo].t - t) <= Math.abs(list[hi].t - t) ? lo : hi;
  }

  function valueAt(list, t) {  // The last value at or before t (step), else null.
    const i = nearest(list, t);
    if (i < 0) return null;
    let j = list[i].t <= t ? i : i - 1;
    if (j < 0) return null;
    return list[j];
  }

  // A crosshair over the plot: pointer and arrow keys, snapping to the series' positions.
  function crosshair(svg, plot, host, positions, x, onMove) {
    const line = s('line', {y1: plot.top, y2: plot.bottom, stroke: C.ink, 'stroke-width': 1,
      opacity: 0.35, visibility: 'hidden', 'pointer-events': 'none'});
    const layer = s('g', {'pointer-events': 'none'});
    const hit = s('rect', {x: plot.left, y: plot.top, width: Math.max(1, plot.right - plot.left),
      height: Math.max(1, plot.bottom - plot.top), fill: 'transparent', tabindex: 0,
      class: 'hit', 'aria-label': 'Chart: move with the arrow keys to read values'});
    svg.append(line, layer, hit);
    const tip = tooltip(host);
    let index = -1;
    function show(i) {
      if (i < 0 || i >= positions.length) return;
      index = i;
      const px = x(positions[i].t);
      line.setAttribute('x1', px);
      line.setAttribute('x2', px);
      line.setAttribute('visibility', 'visible');
      layer.replaceChildren();
      const rows = onMove(positions[i], layer);
      tip.show(px, (plot.top + plot.bottom) / 2, rows);
    }
    function hide() {
      line.setAttribute('visibility', 'hidden');
      layer.replaceChildren();
      tip.hide();
    }
    hit.addEventListener('pointermove', (event) => {
      const box = svg.getBoundingClientRect();
      const scaleX = svg.viewBox.baseVal.width / box.width;
      show(nearest(positions, x.invert((event.clientX - box.left) * scaleX)));
    });
    hit.addEventListener('pointerleave', hide);
    hit.addEventListener('blur', hide);
    hit.addEventListener('focus', () => show(index < 0 ? positions.length - 1 : index));
    hit.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowLeft') show(Math.max(0, index - 1));
      else if (event.key === 'ArrowRight') show(Math.min(positions.length - 1, index + 1));
      else if (event.key === 'Escape') hide();
      else return;
      event.preventDefault();
    });
    return {show, hide};
  }

  function dot(layer, px, py, color, r) {
    layer.append(s('circle', {cx: px, cy: py, r: (r || 4) + 2, fill: '#FFFFFF'}),
      s('circle', {cx: px, cy: py, r: r || 4, fill: color}));
  }

  function linePath(points, x, y, stepWhen) {
    let d = '';
    points.forEach((p, i) => {
      const px = x(p.t).toFixed(1), py = y(p.v).toFixed(1);
      if (i === 0) d += 'M' + px + ' ' + py;
      else if (stepWhen && stepWhen(p)) {
        d += 'H' + px + 'V' + py;
      } else d += 'L' + px + ' ' + py;
    });
    return d;
  }

  // A column with a 4 px rounded data end and a square end at the baseline.
  function column(px, width, y0, y1) {
    const h = Math.abs(y1 - y0);
    const r = Math.min(4, width / 2, h);
    const left = px - width / 2, right = px + width / 2;
    if (y1 <= y0) {  // up
      return 'M' + left + ' ' + y0 + 'V' + (y1 + r) + 'Q' + left + ' ' + y1 + ' ' + (left + r) +
        ' ' + y1 + 'H' + (right - r) + 'Q' + right + ' ' + y1 + ' ' + right + ' ' + (y1 + r) +
        'V' + y0 + 'Z';
    }
    return 'M' + left + ' ' + y0 + 'V' + (y1 - r) + 'Q' + left + ' ' + y1 + ' ' + (left + r) +
      ' ' + y1 + 'H' + (right - r) + 'Q' + right + ' ' + y1 + ' ' + right + ' ' + (y1 - r) +
      'V' + y0 + 'Z';
  }

  function moneyTick(step) {
    const places = step >= 1 ? 0 : 2;
    return (v) => (v < 0 ? MINUS : '') + '$' + Math.abs(v).toLocaleString(US, {
      minimumFractionDigits: places, maximumFractionDigits: places});
  }

  function signedMoneyTick(step) {
    const places = step >= 1 ? 0 : 2;
    return (v) => (Math.abs(v) < step / 1e6 ? '$0' : (v > 0 ? '+' : MINUS) + '$' +
      Math.abs(v).toLocaleString(US, {minimumFractionDigits: places,
        maximumFractionDigits: places}));
  }

  // --- Header and the status line -------------------------------------------------------------

  function renderHeader() {
    const updated = byId('updated');
    const a = doc.account || {};
    const o = doc.overall || {};
    const equity = num(a.equity_usd) !== null ? a.equity_usd : o.equity_usd;
    const parts = [];
    parts.push('Paper account' + (num(equity) !== null ? ' ' + money(equity) : ''));
    parts.push('updated ' + etTime(doc.as_of) + ' ET, ' + etDay(doc.as_of, true));
    if (doc.stale || failures > 2) parts.push('reconnecting');
    const words = parts.join(' · ');
    if (updated.textContent !== words) updated.textContent = words;
  }

  function renderStatus() {
    const st = doc.system;
    const band = byId('status');
    const body = byId('status-body');
    if (!st) {
      band.dataset.state = 'STOPPED';
      put(body, el('span', 'status-text', 'Status unavailable'));
      return;
    }
    band.dataset.state = st.state;
    const left = add(el('div', 'status-main'), el('span', 'status-label', st.label.toUpperCase()),
      el('span', 'status-text', st.text));
    const right = st.rules_since ? el('span', 'status-rules', 'Rules: current set since ' +
      etStamp(st.rules_since) + ' ET') : null;
    put(body, left, right);
  }

  // --- 1. The account: hero figure and KPI tiles ----------------------------------------------

  function delta(name, usd, pct) {
    if (num(usd) === null) return null;
    return add(el('div', 'delta'), el('span', 'delta-label', name),
      el('span', 'delta-value ' + tone(usd), money(usd, true) + (num(pct) !== null ? ' (' +
        signedPct(pct) + ')' : '')));
  }

  function kpi(name, value, sub, cls, extra) {
    return add(el('div', 'kpi'), el('span', 'kpi-label', name), el('span', 'kpi-value ' +
      (cls || ''), value), sub ? el('span', 'kpi-sub', sub) : null, extra || null);
  }

  function meter(fraction, words) {
    const bar = el('div', 'meter');
    bar.setAttribute('role', 'img');
    bar.setAttribute('aria-label', words);
    const fill = el('div', 'meter-fill' + (num(fraction) >= 0.8 ? ' high' : ''));
    fill.style.width = (Math.min(1, Math.max(0, num(fraction) || 0)) * 100).toFixed(1) + '%';
    bar.append(fill);
    return bar;
  }

  function renderAccount() {
    const a = doc.account || {};
    const body = byId('account-body');
    const hero = el('div', 'hero');
    hero.append(el('span', 'hero-label', 'Account equity'));
    if (num(a.equity_usd) === null) {
      hero.append(el('div', 'hero-figure muted', DASH), add(el('div', 'hero-sub'),
        'Equity ', pending()));
    } else {
      hero.append(el('div', 'hero-figure', money(a.equity_usd)));
      const age = num(a.equity_age_seconds);
      const sub = add(el('div', 'hero-sub'), 'as of ' + etTime(a.equity_at, true) + ' ET');
      if (age !== null && age > 600) {
        sub.append(el('span', 'stale', ' · ' + minutesWords(Math.floor(age / 60)) + ' old'));
      }
      hero.append(sub);
    }
    hero.append(add(el('div', 'deltas'), delta('Today', a.today_usd, a.today_pct),
      delta(a.trading_pnl_label || 'Since start', a.since_start_usd, a.since_start_pct)));
    const tiles = el('div', 'kpis');
    const openWords = a.open_trades ? plural(a.open_trades, 'position', 'positions') +
      (num(a.open_pnl_r) !== null ? ' · ' + rText(a.open_pnl_r) : '') +
      (a.open_unpriced ? ' · ' + a.open_unpriced + ' unpriced' : '') : 'No open positions';
    tiles.append(kpi('Open P&L', a.open_trades ? money(a.open_pnl_usd, true) : '$0.00',
      openWords, tone(a.open_pnl_usd)));
    tiles.append(kpi('Realized today', money(a.realized_today_usd, true),
      'closed trades, after verified fees', tone(a.realized_today_usd)));
    const riskWords = num(a.open_risk_cap_pct) !== null ? 'of the ' +
      num(a.open_risk_cap_pct).toFixed(1) + '% cap · ' + money(a.open_risk_usd) : null;
    tiles.append(kpi('Open risk', num(a.open_risk_pct) === null ? DASH :
      num(a.open_risk_pct).toFixed(2) + '%', riskWords, '', num(a.open_risk_fraction) !== null ?
      meter(a.open_risk_fraction, 'Open risk ' + num(a.open_risk_pct).toFixed(2) + '% of a ' +
        num(a.open_risk_cap_pct).toFixed(1) + '% cap') : null));
    tiles.append(kpi('Fees paid', money(a.fees_paid_usd), 'since start, closed trades' +
      (a.fees_pending_trades ? ' · ' + a.fees_pending_trades + ' pending' : '')));
    tiles.append(kpi('Max drawdown', num(a.max_drawdown_pct) === null ? DASH :
      signedPct(a.max_drawdown_pct), num(a.max_drawdown_usd) !== null ?
      money(a.max_drawdown_usd, true) + ' from the peak' : null,
      num(a.max_drawdown_pct) < 0 ? 'neg' : ''));
    const rate = num(a.win_rate);
    const ratio = rate === null ? DASH : add(el('span'), plainPct(rate) + ' · ',
      el('span', tone(a.expectancy_r), rText(a.expectancy_r)));
    const winTile = kpi('Win rate · Expectancy', '', a.stats_trades ? plural(a.stats_trades,
      'closed trade', 'closed trades') + (a.stats_enough ? '' : ', under 30') :
      'no closed trades');
    put(winTile.querySelector('.kpi-value'), ratio);
    tiles.append(winTile);
    put(body, hero, tiles);
  }

  // --- 2. The equity curve, the BTC benchmark and the drawdown ---------------------------------

  function equitySeries() {
    const e = doc.equity || {};
    const pnl = e.mode === 'TRADING_PNL';  // PAGE_EXCLUSION_V2: trading P&L, not equity.
    const points = (e.points || []).map((p) => ({t: Date.parse(p.at),
      v: num(pnl ? p.pnl_usd : p.equity_usd), src: p.basis, trade: p.trade_no}))
      .filter((p) => p.v !== null);
    const bench = ((e.benchmark || {}).points || []).map((p) => ({t: Date.parse(p.at),
      v: num(pnl ? p.pnl_usd : p.value_usd)})).filter((p) => p.v !== null);
    const dd = (e.drawdown || []).map((p) => ({t: Date.parse(p.at), v: num(p.pct),
      src: p.basis}));
    const marks = (e.marks || []).map((m) => ({...m, t: Date.parse(m.at)}));
    return {points, bench, dd, marks, start: pnl ? null : num(e.start_equity_usd), pnl,
      capital: num(e.capital_usd)};
  }

  function inRange(list, t0) {  // Points from t0 on, with the last one before it held at t0.
    if (t0 === null) return list.slice();
    const out = list.filter((p) => p.t >= t0);
    const before = list.filter((p) => p.t < t0).at(-1);
    if (before) out.unshift({...before, t: t0, held: true});
    return out;
  }

  function rangeRow() {
    const row = el('div', 'range-row');
    row.setAttribute('role', 'group');
    row.setAttribute('aria-label', 'Time range');
    for (const [name] of RANGES) {
      const b = el('button', 'range' + (ui.range === name ? ' on' : ''), name);
      b.type = 'button';
      b.setAttribute('aria-pressed', String(ui.range === name));
      b.addEventListener('click', () => {
        ui.range = name;
        drawn.delete('equity');
        render();
      });
      row.append(b);
    }
    return row;
  }

  function renderEquity() {
    const body = byId('equity-body');
    const {points, bench, dd, marks, start, pnl, capital} = equitySeries();
    const meta = byId('equity-meta');
    if (points.length < 2) {
      meta.textContent = '';
      put(body, empty(pnl ? 'Trading P&L starts with the first closed trade.' :
        'The equity curve starts with the first recorded day.'));
      return;
    }
    const now = Math.max(serverNow(), points.at(-1).t);
    const range = RANGES.find(([name]) => name === ui.range)[1];
    const t0 = range === null ? points[0].t : Math.max(points[0].t, now - range);
    const shown = inRange(points, t0);
    const benchShown = inRange(bench, t0);
    const ddShown = inRange(dd, t0);
    const legend = add(el('div', 'legend'),
      add(el('span', 'legend-item'), lineKey(C.account), pnl ? 'Trading P&L' :
        'Account equity'),
      bench.length ? add(el('span', 'legend-item'), lineKey(C.btc), 'BTC buy-and-hold' +
        (pnl && capital ? ' on ' + wholeMoney(capital) : '')) : null);
    const first = (doc.equity || {}).first_snapshot_at;
    const excl = (doc.exclusions || {}).text;
    if (pnl) {
      meta.textContent = 'Closed trades\' P&L after fees, plus open trades' +
        (excl ? ' · ' + excl : '');
    } else {
      meta.textContent = first ? 'Every 5 minutes from ' + etDay(first) + '; earlier days ' +
        'rebuilt from closed trades' : 'Rebuilt from day starts and closed trades';
    }
    const host = el('div', 'chart chart-equity');
    const ddHost = el('div', 'chart chart-dd');
    put(body, add(el('div', 'chart-controls'), rangeRow(), legend), host,
      el('h3', 'chart-title', 'Drawdown from the peak'), ddHost,
      twin('View as table', () => equityTable(shown, benchShown, ddShown)));
    drawEquity(host, ddHost, shown, benchShown, ddShown, marks, start, t0, now, pnl);
  }

  function drawEquity(host, ddHost, pts, bench, dd, marks, start, t0, t1, pnl) {
    const phone = host.clientWidth < 560;
    const margin = {left: phone ? 54 : 64, right: phone ? 12 : 150, top: 12, bottom: 28};
    const {svg, plot} = frame(host, phone ? 240 : 320, margin);
    const values = pts.map((p) => p.v).concat(bench.map((p) => p.v), pnl ? [0] : []);
    const yt = niceTicks(Math.min(...values), Math.max(...values), phone ? 4 : 5);
    const y = scale(yt.lo, yt.hi, plot.bottom, plot.top);
    const x = scale(t0, t1, plot.left, plot.right);
    yAxis(svg, plot, y, yt.ticks, pnl ? signedMoneyTick(yt.step) : moneyTick(yt.step));
    // New York day boundaries: faint hairlines (only while each day is wide enough to read).
    if ((t1 - t0) / DAY <= 45) {
      let m = etMidnight(t0 + DAY + 2 * HOUR);
      const g = s('g');
      while (m < t1) {
        if (m > t0) g.append(s('line', {x1: Math.round(x(m)) + 0.5, x2: Math.round(x(m)) + 0.5,
          y1: plot.top, y2: plot.bottom, stroke: C.grid}));
        m = etMidnight(m + DAY + 2 * HOUR);
      }
      svg.append(g);
    }
    const tt = timeTicks(t0, t1, plot.right - plot.left);
    xAxis(svg, plot, x, tt.ticks, tt.step);
    const step = (p) => p.src === 'RECONSTRUCTED' || p.src === 'REALIZED';
    const line = linePath(pts, x, y, step);
    const floor = pnl ? y(0) : plot.bottom;  // A P&L wash runs to the zero line.
    const area = line + 'V' + floor.toFixed(1) + 'H' + x(pts[0].t).toFixed(1) + 'Z';
    if (pnl) {
      svg.append(s('line', {x1: plot.left, x2: plot.right, y1: Math.round(y(0)) + 0.5,
        y2: Math.round(y(0)) + 0.5, stroke: C.ink, 'stroke-width': 1}));
    }
    svg.append(s('path', {d: area, fill: C.account, 'fill-opacity': 0.1, stroke: 'none'}));
    if (bench.length > 1) {
      svg.append(s('path', {d: linePath(bench, x, y), fill: 'none', stroke: C.btc,
        'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round'}));
    }
    svg.append(s('path', {d: line, fill: 'none', stroke: C.account, 'stroke-width': 2,
      'stroke-linejoin': 'round', 'stroke-linecap': 'round'}));
    // Halts and soft limits: 8 px markers with a 2 px surface ring, on the equity line.
    const shownMarks = marks.filter((m) => m.t >= t0 && m.t <= t1);
    for (const m of shownMarks) {
      const at = valueAt(pts, m.t);
      if (!at) continue;
      dot(svg, x(m.t), y(at.v), m.kind === 'DAILY_HALT' ? C.loss : C.warn, 4);
    }
    // Direct end labels (text tokens; the line key carries the colour), leader lines if close.
    if (!phone) {
      const ends = [{name: pnl ? 'Trading' : 'Account', v: pts.at(-1).v, color: C.account}];
      if (bench.length > 1) ends.push({name: 'BTC hold', v: bench.at(-1).v, color: C.btc});
      const placed = ends.map((e) => ({...e, y: y(e.v), ly: y(e.v)}));
      if (placed.length === 2 && Math.abs(placed[0].ly - placed[1].ly) < 18) {
        const mid = (placed[0].ly + placed[1].ly) / 2;
        const upper = placed[0].ly <= placed[1].ly ? 0 : 1;
        placed[upper].ly = mid - 9;
        placed[1 - upper].ly = mid + 9;
      }
      for (const e of placed) {
        const g = s('g');
        g.append(s('path', {d: 'M' + (plot.right + 2) + ' ' + e.y + 'L' + (plot.right + 8) +
          ' ' + e.ly, stroke: e.color, 'stroke-width': 1, fill: 'none'}));
        g.append(s('line', {x1: plot.right + 10, x2: plot.right + 20, y1: e.ly, y2: e.ly,
          stroke: e.color, 'stroke-width': 2}));
        g.append(s('text', {x: plot.right + 24, y: e.ly + 4, class: 'end-label'},
          e.name + ' ' + (pnl ? money(e.v, true, 0) : wholeMoney(e.v))));
        svg.append(g);
      }
    }
    host.append(svg);
    // The drawdown chart: its own axis, the same time scale.
    const ddMargin = {...margin, top: 8, bottom: 24};
    const dframe = frame(ddHost, phone ? 96 : 120, ddMargin);
    const dmin = Math.min(-0.5, ...dd.map((p) => p.v));
    const dt = niceTicks(dmin, 0, 2);
    const dy = scale(dt.lo, 0, dframe.plot.bottom, dframe.plot.top);
    const places = (String(dt.step).split('.')[1] || '').length;
    yAxis(dframe.svg, dframe.plot, dy, dt.ticks.filter((v) => v <= 0), (v) => (v < 0 ? MINUS :
      '') + Math.abs(v).toFixed(places) + '%');
    const dx = scale(t0, t1, dframe.plot.left, dframe.plot.right);
    const ddLine = linePath(dd, dx, dy, (p) => p.src === 'RECONSTRUCTED' ||
      p.src === 'REALIZED');
    if (dd.length > 1) {
      dframe.svg.append(s('path', {d: ddLine + 'V' + dy(0) + 'H' + dx(dd[0].t).toFixed(1) + 'Z',
        fill: C.loss, 'fill-opacity': 0.1, stroke: 'none'}));
      dframe.svg.append(s('path', {d: ddLine, fill: 'none', stroke: C.loss, 'stroke-width': 2,
        'stroke-linejoin': 'round'}));
    }
    dframe.svg.append(s('line', {x1: dframe.plot.left, x2: dframe.plot.right,
      y1: Math.round(dy(0)) + 0.5, y2: Math.round(dy(0)) + 0.5, stroke: C.axis}));
    ddHost.append(dframe.svg);
    // One crosshair for both charts.
    const tol = (t1 - t0) / 120;
    const cross = crosshair(svg, plot, host, pts, x, (p, layer) => {
      dot(layer, x(p.t), y(p.v), C.account);
      const rows = [el('div', 'tip-time', etDay(p.t, true) + ' ' + etTime(p.t) + ' ET')];
      rows.push(pnl ? tipRow(money(p.v, true), 'trading P&L', C.account, 'strong ' + tone(p.v))
        : tipRow(money(p.v), 'Account equity', C.account, 'strong'));
      if (start) {
        rows.push(tipRow(money(p.v - start, true) + ' (' + signedPct((p.v - start) / start *
          100) + ')', 'vs the start', null, tone(p.v - start)));
      }
      const b = valueAt(bench, p.t);
      if (b && !b.held) {
        dot(layer, x(p.t), y(b.v), C.btc);
        rows.push(tipRow(pnl ? money(b.v, true) : money(b.v), 'BTC buy-and-hold', C.btc,
          pnl ? tone(b.v) : ''));
      }
      const d = valueAt(dd, p.t);
      if (d) {
        rows.push(tipRow(signedPct(d.v), 'drawdown', null, d.v < 0 ? 'neg' : ''));
        ddDot(d);
      }
      if (p.src === 'RECONSTRUCTED') rows.push(el('div', 'tip-note', 'Reconstructed from ' +
        'closed trades' + (p.trade ? ' (#' + p.trade + ' closed)' : ' (day start)')));
      if (p.src === 'ACCOUNT_READ') rows.push(el('div', 'tip-note', 'Account read at a risk ' +
        'check'));
      if (p.src === 'REALIZED') rows.push(el('div', 'tip-note', p.trade ? '#' + p.trade +
        ' closed: realized P&L to date' : 'Start'));
      if (p.src === 'SNAPSHOT' && pnl) rows.push(el('div', 'tip-note', 'Realized to date ' +
        'plus open trades at the account snapshot'));
      if (p.src === 'NOW') rows.push(el('div', 'tip-note', 'Now: realized plus open trades'));
      for (const m of shownMarks) {
        if (Math.abs(m.t - p.t) <= tol) rows.push(el('div', 'tip-note tip-mark', etTime(m.t) +
          ' ET · ' + m.text));
      }
      return rows;
    });
    let ddLayer = s('g', {'pointer-events': 'none'});
    dframe.svg.append(ddLayer);
    function ddDot(d) {
      ddLayer.replaceChildren();
      dot(ddLayer, dx(d.t), dy(d.v), C.loss);
    }
    svg.querySelector('.hit').addEventListener('pointerleave', () => ddLayer.replaceChildren());
    return cross;
  }

  function equityTable(pts, bench, dd) {
    const pnl = (doc.equity || {}).mode === 'TRADING_PNL';
    const {wrap, body} = table([['Time (ET)', 'l'], [pnl ? 'Trading P&L' : 'Equity', 'r'],
      ['BTC buy-and-hold', 'r'], ['Drawdown', 'r'], ['Source', 'l']], 560, 'data');
    const rows = pts.filter((p) => !p.held);
    const every = Math.max(1, Math.ceil(rows.length / 200));
    const picked = rows.filter((p, i) => i % every === 0 || i === rows.length - 1).reverse();
    for (const p of picked) {
      const b = valueAt(bench, p.t), d = valueAt(dd, p.t);
      const tr = el('tr');
      tr.append(td(n(etDay(p.t) + ' ' + etTime(p.t))), td(n(money(p.v, pnl)), 'r'),
        td(n(b && !b.held ? money(b.v, pnl) : DASH), 'r muted'),
        td(n(d ? signedPct(d.v) : DASH), 'r'), td({RECONSTRUCTED: 'Rebuilt from closed trades',
          SNAPSHOT: pnl ? 'Realized + open at a snapshot' : 'Account snapshot',
          ACCOUNT_READ: 'Risk-check read', REALIZED: 'Realized (closed trades)',
          NOW: 'Now (realized + open)'}[p.src] || DASH,
        'muted'));
      body.append(tr);
    }
    const note = every > 1 ? el('p', 'note', 'Every ' + every + 'th point; the JSON has all.') :
      null;
    return add(el('div'), wrap, note);
  }

  // --- 3. Daily P&L, the R distribution and performance ---------------------------------------

  function renderPerformance() {
    const body = byId('performance-body');
    const p = doc.performance;
    if (!p) {
      put(body, empty('No closed trades yet.'));
      return;
    }
    const daily = doc.daily || [];
    if (!daily.length && !p.r_histogram.trades) {
      put(body, empty('No closed trades yet.'));
      return;
    }
    const left = el('div', 'panel-chart');
    left.append(add(el('div', 'chart-head'), el('h3', 'chart-title', 'Daily P&L'),
      el('span', 'chart-sub', 'net after verified fees, by exit day')));
    const dailyHost = el('div', 'chart chart-daily');
    left.append(dailyHost, twin('View as table', () => dailyTable(daily)));
    const right = el('div', 'panel-chart');
    const h = p.r_histogram;
    right.append(add(el('div', 'chart-head'), el('h3', 'chart-title', 'R-multiple distribution'),
      el('span', 'chart-sub', plural(h.trades, 'closed trade', 'closed trades') +
        ', 0.25R bins')));
    const histHost = el('div', 'chart chart-hist');
    right.append(histHost, twin('View as table', () => histTable(h)));
    const charts = add(el('div', 'chart-pair'), left, right);
    put(body, charts, performanceTable(p));
    if (daily.length) drawDaily(dailyHost, daily);
    else dailyHost.append(empty('No closed trades yet.'));
    if (h.trades) drawHistogram(histHost, h);
    else histHost.append(empty('No closed trades yet.'));
    const meta = byId('performance-meta');
    meta.textContent = '';
  }

  function drawDaily(host, days) {
    const phone = host.clientWidth < 560;
    const margin = {left: phone ? 50 : 58, right: 12, top: 22, bottom: 28};
    const {svg, plot} = frame(host, phone ? 200 : 240, margin);
    const vals = days.map((d) => num(d.net_usd) || 0);
    const vmin = Math.min(0, ...vals), vmax = Math.max(0, ...vals);
    const room = (vmax - vmin) * 0.12;  // Space for the best and worst days' labels.
    const yt = niceTicks(vmin < 0 ? vmin - room : 0, vmax > 0 ? vmax + room : 0, 4);
    const y = scale(yt.lo, yt.hi, plot.bottom, plot.top);
    yAxis(svg, plot, y, yt.ticks, signedMoneyTick(yt.step));
    const band = (plot.right - plot.left) / days.length;
    const width = Math.max(2, Math.min(24, band - 2));
    const zero = Math.round(y(0)) + 0.5;
    const best = vals.indexOf(Math.max(...vals)), worst = vals.indexOf(Math.min(...vals));
    const g = s('g');
    const marks = [];
    days.forEach((d, i) => {
      const v = vals[i];
      const px = plot.left + band * (i + 0.5);
      if (d.trades && Math.abs(y(v) - zero) >= 0.5) {
        const bar = s('path', {d: column(px, width, zero, y(v)), fill: v >= 0 ? C.gain : C.loss,
          class: 'bar'});
        g.append(bar);
        marks.push([bar, d]);
      } else if (d.trades) {
        g.append(s('line', {x1: px - width / 2, x2: px + width / 2, y1: zero, y2: zero,
          stroke: C.muted, 'stroke-width': 2}));
      }
      if (d.trades && (i === best || i === worst) && v !== 0) {
        // On the cap; when the cap is too near the axis below, beside the cap instead.
        const below = v < 0 && y(v) + 16 > plot.bottom;
        const ly = below ? y(v) - 2 : v >= 0 ? y(v) - 6 : y(v) + 13;
        const side = px > (plot.left + plot.right) / 2 ? -1 : 1;
        g.append(s('text', {x: below ? px + side * (width / 2 + 5) : px, y: ly,
          'text-anchor': below ? (side < 0 ? 'end' : 'start') : 'middle', class: 'cap-label'},
        money(v, true)));
      }
    });
    svg.append(g);
    svg.append(s('line', {x1: plot.left, x2: plot.right, y1: zero, y2: zero, stroke: C.ink,
      'stroke-width': 1}));
    // Day labels: at most one per ~56 px.
    const every = Math.max(1, Math.ceil(56 / band));
    const axis = s('g', {class: 'axis'});
    days.forEach((d, i) => {
      if ((days.length - 1 - i) % every) return;
      axis.append(s('text', {x: plot.left + band * (i + 0.5), y: plot.bottom + 18,
        'text-anchor': 'middle', class: 'tick'}, dayLabel(d.day)));
    });
    svg.append(axis);
    host.append(svg);
    barTips(svg, host, days.map((d, i) => ({x0: plot.left + band * i, x1: plot.left + band *
      (i + 1), d})), plot, (item) => {
      const d = item.d;
      const rows = [el('div', 'tip-time', dayLabel(d.day, true))];
      if (!d.trades) {
        rows.push(el('div', 'tip-note', 'No trade closed'));
        return rows;
      }
      rows.push(tipRow(money(d.net_usd, true), 'net P&L', null, 'strong ' + tone(d.net_usd)));
      if (num(d.gross_usd) !== null) rows.push(tipRow(money(d.gross_usd, true), 'gross'));
      if (num(d.fees_usd) !== null) rows.push(tipRow(money(d.fees_usd), 'fees'));
      rows.push(tipRow(d.won + ' of ' + d.trades, 'won'));
      if (d.market) rows.push(el('div', 'tip-note', d.market));
      if (d.fees_pending) rows.push(el('div', 'tip-note', plural(d.fees_pending, 'trade',
        'trades') + ' with fees pending: gross shown for those'));
      if (d.excluded) rows.push(el('div', 'tip-note', 'Excluded from the statistics'));
      return rows;
    });
  }

  // Per-mark tooltips for columns: the whole band is the hit target (≥24 px when it can be).
  function barTips(svg, host, items, plot, rows) {
    const tip = tooltip(host);
    const layer = s('g');
    svg.append(layer);
    for (const item of items) {
      const hit = s('rect', {x: item.x0, y: plot.top, width: Math.max(1, item.x1 - item.x0),
        height: plot.bottom - plot.top + 20, fill: 'transparent', tabindex: 0, class: 'hit'});
      const show = () => {
        hit.setAttribute('fill', 'rgba(17,20,24,0.04)');
        tip.show((item.x0 + item.x1) / 2 * (host.clientWidth / svg.viewBox.baseVal.width),
          (plot.top + plot.bottom) / 2, rows(item));
      };
      const hide = () => {
        hit.setAttribute('fill', 'transparent');
        tip.hide();
      };
      hit.addEventListener('pointerenter', show);
      hit.addEventListener('pointerleave', hide);
      hit.addEventListener('focus', show);
      hit.addEventListener('blur', hide);
      layer.append(hit);
    }
  }

  function dailyTable(days) {
    const {wrap, body} = table([['Day', 'l'], ['Trades', 'r'], ['Won', 'r'], ['Gross', 'r'],
      ['Fees', 'r'], ['Net', 'r'], ['Market', 'l']], 640, 'data');
    for (const d of [...days].reverse()) {
      const tr = el('tr');
      tr.append(td(add(el('span'), dayLabel(d.day, true), d.excluded ? el('span', 'tag',
        'excluded from stats') : null)), td(n(String(d.trades)), 'r'),
      td(n(String(d.won)), 'r'), td(n(d.trades ? money(d.gross_usd, true) : DASH), 'r'),
      td(n(d.trades ? money(d.fees_usd) : DASH), 'r muted'),
      td(n(d.trades ? money(d.net_usd, true) : DASH), 'r ' + tone(d.net_usd)),
      td(d.market || DASH, 'muted'));
      body.append(tr);
    }
    return wrap;
  }

  function binLabel(b) {
    const lo = num(b.lo), hi = num(b.hi);
    if (b.open_low) return 'below ' + rText(hi);
    if (b.open_high) return rText(lo) + ' or more';
    return rText(lo) + ' to ' + rText(hi);
  }

  function drawHistogram(host, h) {
    const phone = host.clientWidth < 560;
    const margin = {left: phone ? 34 : 40, right: 12, top: 30, bottom: 28};
    const {svg, plot} = frame(host, phone ? 200 : 240, margin);
    const lo = num(h.low), hi = num(h.high);
    const x = scale(lo, hi, plot.left, plot.right);
    const counts = h.bins.map((b) => b.count);
    const top = Math.max(1, ...counts);
    const yt = niceTicks(0, top, 4);
    const ticks = yt.ticks.filter((v) => Number.isInteger(v) && v <= top * 1.12);
    const y = scale(0, top * 1.12, plot.bottom, plot.top);
    yAxis(svg, plot, y, ticks, (v) => String(v));
    const g = s('g');
    for (const b of h.bins) {
      if (!b.count) continue;
      const x0 = x(num(b.lo)), x1 = x(num(b.hi));
      const w = Math.min(24, x1 - x0 - 2);
      g.append(s('path', {d: column((x0 + x1) / 2, w, plot.bottom, y(b.count)),
        fill: C.account, class: 'bar'}));
    }
    svg.append(g);
    const axis = s('g', {class: 'axis'});
    axis.append(s('line', {x1: plot.left, x2: plot.right, y1: plot.bottom + 0.5,
      y2: plot.bottom + 0.5, stroke: C.axis}));
    for (let v = Math.ceil(lo); v <= hi; v += 1) {
      axis.append(s('text', {x: x(v), y: plot.bottom + 18, 'text-anchor': 'middle',
        class: 'tick'}, (v > 0 ? '+' : v < 0 ? MINUS : '') + Math.abs(v) + 'R'));
    }
    svg.append(axis);
    // Zero, and the expectancy as a labelled hairline.
    svg.append(s('line', {x1: Math.round(x(0)) + 0.5, x2: Math.round(x(0)) + 0.5, y1: plot.top,
      y2: plot.bottom, stroke: C.ink, 'stroke-width': 1}));
    const ex = num(h.expectancy_r);
    if (ex !== null) {
      const px = Math.round(x(Math.min(hi, Math.max(lo, ex)))) + 0.5;
      svg.append(s('line', {x1: px, x2: px, y1: plot.top - 14, y2: plot.bottom, stroke: C.ink,
        'stroke-width': 1, opacity: 0.55}));
      const right = px > (plot.left + plot.right) / 2;
      svg.append(s('text', {x: px + (right ? -5 : 5), y: plot.top - 16, class: 'cap-label',
        'text-anchor': right ? 'end' : 'start'}, 'Expectancy ' + rText(ex)));
    }
    host.append(svg);
    barTips(svg, host, h.bins.map((b) => ({x0: x(num(b.lo)), x1: x(num(b.hi)), b})), plot,
      (item) => {
        const b = item.b;
        const rows = [el('div', 'tip-time', binLabel(b))];
        rows.push(tipRow(plural(b.count, 'trade', 'trades'), null, null, 'strong'));
        if (b.trade_nos.length) rows.push(el('div', 'tip-note', b.trade_nos.slice(0, 12)
          .map((t) => '#' + t).join(', ') + (b.trade_nos.length > 12 ? ' …' : '')));
        return rows;
      });
  }

  function histTable(h) {
    const {wrap, body} = table([['R', 'l'], ['Trades', 'r'], ['Trade numbers', 'l']], 420,
      'data');
    for (const b of h.bins) {
      if (!b.count) continue;
      const tr = el('tr');
      tr.append(td(n(binLabel(b))), td(n(String(b.count)), 'r'),
        td(b.trade_nos.map((t) => '#' + t).join(', '), 'muted'));
      body.append(tr);
    }
    return add(el('div'), wrap, el('p', 'note', 'Expectancy ' + rText(h.expectancy_r) +
      ' over ' + plural(h.trades, 'trade', 'trades') + '.'));
  }

  function performanceTable(p) {
    const box = el('div', 'perf');
    const {wrap, body} = table([['', 'l'], ['Trades', 'r'], ['Win rate', 'r'],
      ['Expectancy', 'r'], ['Avg win', 'r'], ['Avg loss', 'r'], ['Profit factor', 'r'],
      ['Max drawdown', 'r'], ['Fees', 'r']], 880, 'perf-table');
    for (const row of p.rows) {
      const tr = el('tr');
      let when = null;
      if (row.row === 'current' && row.since) when = 'from ' + etDay(row.since);
      tr.append(td(add(el('span'), row.label, when ? el('span', 'muted', ' · ' + when) : null)));
      tr.append(td(n(String(row.trades)), 'r'));
      if (!row.trades) {
        tr.append(td('No trades yet', 'muted', 7));
      } else if (!row.enough) {
        tr.append(td(row.wins + ' won, ' + row.losses + ' lost · ratios from ' +
          p.minimum_sample + ' trades', 'muted counts', 5));
        tr.append(td(n(num(row.max_drawdown_pct) === null ? DASH :
          signedPct(row.max_drawdown_pct)), 'r'), td(n(money(row.fees_usd)), 'r'));
      } else {
        tr.append(td(n(plainPct(row.win_rate)), 'r'),
          td(n(rText(row.expectancy_r)), 'r ' + tone(row.expectancy_r)),
          td(n(rText(row.avg_win_r)), 'r'), td(n(rText(row.avg_loss_r)), 'r'),
          td(n(num(row.profit_factor) === null ? DASH : num(row.profit_factor).toFixed(2)), 'r'),
          td(n(num(row.max_drawdown_pct) === null ? DASH : signedPct(row.max_drawdown_pct)), 'r'),
          td(n(money(row.fees_usd)), 'r'));
      }
      body.append(tr);
    }
    box.append(wrap);
    const notes = [];
    if (p.exclusions && p.exclusions.text) notes.push(p.exclusions.text);
    if (p.untagged) notes.push(plural(p.untagged, 'trade', 'trades') + ' on days without a ' +
      'market tag yet');
    if (notes.length) box.append(el('p', 'note', notes.join(' · ')));
    return box;
  }

  // --- 4. Open positions ------------------------------------------------------------------------

  function positionBar(t) {  // Stop (left) to target (right), the entry and the price now.
    const p = t.position;
    if (!p) return null;
    const mark = num(p.mark), entry = num(p.entry);
    const clamp = (f) => (Math.min(1, Math.max(0, num(f))) * 100).toFixed(2) + '%';
    const svg = s('svg', {width: '100%', height: 30, class: 'pos-svg', role: 'img',
      'aria-label': 'Stop ' + price(p.stop) + ', entry ' + price(p.entry) + (mark !== null ?
        ', price ' + price(mark) : '') + ', target ' + price(p.target)});
    const track = svg;  // The box's side padding keeps the end ticks and the dot inside.
    track.append(s('rect', {x: 0, y: 13, width: '100%', height: 4, fill: '#F1F3F5'}));
    if (mark !== null && entry !== null) {
      const a = Math.min(1, Math.max(0, num(p.entry_at_fraction)));
      const b = Math.min(1, Math.max(0, num(p.mark_fraction)));
      track.append(s('rect', {x: (Math.min(a, b) * 100).toFixed(2) + '%', y: 13,
        width: (Math.abs(b - a) * 100).toFixed(2) + '%', height: 4,
        fill: mark >= entry ? C.gain : C.loss, 'fill-opacity': 0.55}));
    }
    track.append(s('rect', {x: 0, y: 6, width: 2, height: 18, fill: C.loss}));
    track.append(s('rect', {x: '100%', y: 6, width: 2, height: 18, fill: C.gain,
      transform: 'translate(-2 0)'}));
    track.append(s('rect', {x: clamp(p.entry_at_fraction), y: 8, width: 2, height: 14,
      fill: C.ink, transform: 'translate(-1 0)'}));
    if (mark !== null) {
      track.append(s('circle', {cx: clamp(p.mark_fraction), cy: 15, r: 6, fill: '#FFFFFF'}),
        s('circle', {cx: clamp(p.mark_fraction), cy: 15, r: 4.5,
          fill: mark >= entry ? C.gain : C.loss}));
    }
    const box = el('div', 'pos-bar');
    box.append(svg, add(el('div', 'pos-ends'), n('Stop ' + price(p.stop), 'end-stop'),
      n('Entry ' + price(p.entry), 'end-entry'), n('Target ' + price(p.target), 'end-target')));
    return box;
  }

  function renderOpen() {
    const body = byId('open-body');
    const trades = doc.live_trades;
    byId('open-meta').textContent = trades.length ? plural(trades.length, 'position',
      'positions') : '';
    if (!trades.length) {
      put(body, empty('No open positions.'));
      return;
    }
    const list = el('div', 'positions');
    for (const t of trades) {
      const p = t.position || {};
      const row = el('article', 'position');
      const head = add(el('div', 'pos-head'), add(el('div', 'pos-coin'), tradeLink(t),
        el('span', 'pos-side', 'Long'), n('#' + t.trade_no, 'muted')),
      add(el('div', 'pos-pnl'), n(money(t.pnl_usd, true), 'pos-usd ' + tone(t.pnl_usd)),
        n(rText(t.pnl_r), 'pos-r ' + tone(t.pnl_r))));
      const facts = el('dl', 'pos-facts');
      const fact = (term, value) => facts.append(el('dt', null, term), add(el('dd'),
        value instanceof Node ? value : n(value)));
      fact('Size', quantity(t.qty) + ' ' + base(t.symbol) + ' · ' + money(t.value_usd ||
        t.entry_value_usd));
      fact('Entry', price(t.entry));
      fact('Price', add(el('span'), n(price(p.mark !== undefined ? p.mark : t.price)),
        p.mark_basis === 'PUBLIC_QUOTE' ? el('span', 'tag', 'live bid') : p.mark_at ?
          el('span', 'tag', ago(p.mark_at)) : null));
      fact('In trade', minutesWords(t.minutes_in_trade));
      fact('Stop · target', price(t.stop) + ' · ' + price(t.target));
      const distance = num(p.r_to_stop) !== null ? rText(p.r_to_stop, 1) + ' to stop · ' +
        rText(p.r_to_target, 1) + ' to target' : null;
      row.append(head, facts, positionBar(t));
      if (distance) row.append(n(distance, 'pos-distance'));
      const check = t.jev_check || (t.jev_last ? {text: t.jev_last.text, at: t.jev_last.at} :
        null);
      if (check) {
        row.append(add(el('div', 'pos-jev'), el('span', 'who who-jev', 'Jev'),
          el('span', null, check.text + ' · ' + etStamp(check.at) + ' ET')));
      }
      if (t.closing) row.append(el('div', 'pos-closing', 'Closing'));
      list.append(row);
    }
    put(body, list);
  }

  // --- Risk limits and the market ---------------------------------------------------------------

  function limitsTile() {
    const l = doc.limits || {};
    const tile = el('div', 'tile');
    const head = add(el('div', 'tile-head'), label('Today vs limits'),
      num(l.day_start_equity_usd) !== null ? el('span', 'tile-meta', 'day start ' +
        money(l.day_start_equity_usd)) : null);
    tile.append(head, n(money(l.pnl_usd, true), 'big ' + tone(l.pnl_usd)));
    const sub = [];
    if (l.measure === 'ACCOUNT_EQUITY') {
      sub.push('account equity ' + money(l.account_equity_usd) + ' at ' + etStamp(l.equity_at) +
        ' ET');
    } else {
      sub.push('realized ' + money(l.realized_usd, true), 'open ' + money(l.open_usd, true));
      if (l.open_unpriced) sub.push(plural(l.open_unpriced, 'open trade', 'open trades') +
        ' unpriced');
      if (l.fees_pending) sub.push('fees pending');
    }
    tile.append(el('div', 'sub', sub.join(' · ')));
    if (l.baseline_corrected_at) {
      tile.append(el('div', 'sub', 'Day start corrected at ' + etTime(l.baseline_corrected_at) +
        ' ET' + (num(l.baseline_corrected_from_usd) !== null ? ', from ' +
          money(l.baseline_corrected_from_usd) : '')));
    }
    if (num(l.hard_at) === null) {
      tile.append(add(el('div', 'sub'), 'Limits ', pending()));
      return tile;
    }
    const bar = el('div', 'limit-bar');
    bar.setAttribute('role', 'img');
    bar.setAttribute('aria-label', 'Loss today ' + signedPct(l.pnl_pct) + ' of day-start ' +
      'equity; soft limit −' + l.soft_pct + '%, hard limit −' + l.hard_pct + '%');
    const fill = el('div', 'fill neg-bg');
    fill.style.width = (num(l.loss_fraction) * 100).toFixed(2) + '%';
    add(bar, el('div', 'track'), fill);
    const marks = [];
    if (num(l.soft_at) !== null) marks.push([l.soft_at, MINUS + num(l.soft_pct) +
      '% no new entries', 'soft']);
    marks.push([l.hard_at, MINUS + num(l.hard_pct) + '% sell all', 'hard']);
    for (const [at, words, cls] of marks) {
      const left = (num(at) * 100).toFixed(2) + '%';
      const tick = el('div', 'tick ' + cls);
      tick.style.left = left;
      const tag = el('div', 'tick-label ' + cls, words);
      tag.style.left = left;
      bar.append(tick, tag);
    }
    tile.append(bar);
    return tile;
  }

  function marketTile() {
    const m = doc.market || {};
    const tile = el('div', 'tile');
    const title = !m.day || m.is_today ? 'Market today' : 'Market, ' + dayLabel(m.day, true);
    tile.append(label(title));
    if (!m.day) {
      tile.append(add(el('div', 'sub'), 'Market tag ', pending()));
    } else {
      tile.append(el('div', 'market-words', m.words || m.tag));
      const chips = el('div', 'chips');
      if (m.selloff === true && m.worst_hour_at) {
        chips.append(el('span', 'chip chip-loss', 'Sell-off ' + etTime(m.worst_hour_at) +
          ' ET: worst hour ' + signedPct(m.worst_hour_pct, 1)));
      } else if (m.selloff === false && num(m.worst_hour_pct) !== null) {
        chips.append(el('span', 'chip', 'No sell-off · worst hour ' +
          signedPct(m.worst_hour_pct, 1)));
      }
      if (chips.childNodes.length) tile.append(chips);
    }
    if (m.pacing_rule) {
      tile.append(el('div', 'sub', 'Entries pause while the median coin is down 2% in an ' +
        'hour.'));
    }
    return tile;
  }

  function renderTiles() {
    put(byId('tiles-body'), limitsTile(), marketTile());
  }

  // --- Research agent and Jev -------------------------------------------------------------------

  function dl(pairs) {
    const list = el('dl', 'facts');
    for (const [term, value] of pairs) {
      if (value === null || value === undefined) continue;
      list.append(el('dt', null, term), add(el('dd'), value));
    }
    return list;
  }

  function panel(title, statusWords, statusCls, pairs) {
    const box = el('div', 'panel');
    const head = add(el('div', 'panel-head'), el('h2', null, title),
      statusWords ? el('span', 'panel-status ' + (statusCls || ''), statusWords) : null);
    box.append(head, dl(pairs));
    return box;
  }

  function researchPanel() {
    const a = doc.agents || {};
    const card = (a.research || [])[0];
    const schedule = a.schedule;
    if (!card) return panel('Research agent', 'no runs yet', 'muted', []);
    const picks = (card.picks_list || []).map((p) => base(p.symbol));
    const shown = picks.slice(0, 5).join(', ') + (picks.length > 5 ? ' +' +
      (picks.length - 5) + ' more' : '');
    let last = etTime(card.last_run_at) + ' slot';
    if (card.received_at) last += ', sent ' + etStamp(card.received_at) + ' ET';
    last += ' · ' + plural(card.picks || 0, 'pick', 'picks') + (shown ? ' (' + shown + ')' : '');
    let next = null;
    if (schedule && schedule.next_run_at) {
      next = etStamp(schedule.next_run_at) + ' ET';
      if (schedule.next_full_run_at && schedule.next_full_run_at !== schedule.next_run_at) {
        next += ' · full run at ' + etTime(schedule.next_full_run_at) + ' ET';
      }
    }
    const started = doc.status && doc.status.started_at;
    return panel('Research agent', card.received_at ? '● report ' + ago(card.received_at) : null,
      'ok', [['Agent', card.agent], ['Last run', last], ['Next run', next],
        ['Runs', a.runs_total + (started ? ' since ' + etDay(started) : '')]]);
  }

  function jevPanel() {
    const j = (doc.agents || {}).jev || {};
    const health = {OK: ['● healthy', 'ok'], 'Breaker open': ['● breaker open', 'bad'],
      Unavailable: ['● unavailable', 'bad'], 'No calls yet': ['no calls yet', 'muted']}[j.health]
      || [j.health, 'muted'];
    const sel = j.latest_selection;
    let review = null;
    if (sel) {
      review = 'Selected ' + sel.selected + ' of ' + sel.picks;
      if (sel.symbols && sel.symbols.length) {
        review += ': ' + sel.symbols.slice(0, 5).join(', ') + (sel.symbols.length > 5 ?
          ' +' + (sel.symbols.length - 5) + ' more' : '');
      }
    }
    const failed = j.failed_today ? plural(j.failed_today, 'review', 'reviews') +
      (j.failed_today_more ? '+' : '') + ' failed' : 'none failed';
    const breaker = {CLOSED: 'Closed', OPEN: 'Open', HALF_OPEN: 'Half open'}[j.breaker] || null;
    const reviews = {ENABLED: 'On', DISABLED: 'Off'}[j.reviews] || null;
    return panel('Jev, the judge', health[0], health[1], [['Latest pick review', review],
      ['Calls today', (j.calls_today || 0) + ', ' + failed], ['Trade reviews', reviews],
      ['Breaker', breaker]]);
  }

  function decisionsList() {
    const items = (doc.feed || []).slice(0, DECISIONS_SHOWN);
    const box = el('div', 'decisions');
    box.append(el('h3', 'label', 'Latest decisions'));
    if (!items.length) {
      box.append(el('p', 'muted', 'No decisions yet.'));
      return box;
    }
    const list = el('ol', 'decision-list');
    for (const d of items) {
      const who = d.actor === 'JEV' ? ['Jev', 'who-jev'] : d.actor === 'RESEARCH_AGENT' ?
        [d.agent || 'Agent', 'who-agent'] : ['App', 'who-app'];
      const time = d.repeats > 1 && d.since ? span(d.since, d.at) : etStamp(d.at);
      list.append(add(el('li'), n(time, 'when'), el('span', 'who ' + who[1], who[0]),
        add(el('span', 'what'), d.text, d.note ? el('span', 'muted', ' · ' + d.note) : null)));
    }
    box.append(list);
    return box;
  }

  function renderAgents() {
    put(byId('agents-body'), add(el('div', 'panels'), researchPanel(), jevPanel()),
      decisionsList());
  }

  // --- Closed today and past days -------------------------------------------------------------

  function renderClosed() {
    const body = byId('closed-body');
    const today = etDayKey(doc.as_of);
    const trades = doc.past.closed_trades.filter((t) => t.exit_at && etDayKey(t.exit_at) ===
      today);
    byId('closed-meta').textContent = trades.length ? 'Select a coin for its full story' : '';
    if (!trades.length) {
      put(body, empty('No trades closed today.'));
      return;
    }
    const {wrap, body: tbody} = table([['#', 'l'], ['Coin', 'l'], ['Managed by', 'l'],
      ['In → out (ET)', 'l'], ['Entry', 'r'], ['Stop', 'r'], ['Target', 'r'], ['Exit', 'r'],
      ['Why it ended', 'l'], ['P&L', 'r'], ['R', 'r']], 1060, 'trades');
    for (const t of trades) {
      const tr = el('tr');
      tr.append(td(n('#' + t.trade_no), 'muted'), td(tradeLink(t), 'strong'),
        td(armWords(t), 'muted'), td(n(etTime(t.entry_at) + ' → ' + etTime(t.exit_at))),
        td(n(price(t.entry)), 'r'), td(n(price(t.planned_stop)), 'r muted'),
        td(n(price(t.planned_target)), 'r muted'), td(n(price(t.exit)), 'r'),
        td(t.exit_reason || DASH), td(n(money(t.pnl_usd, true)), 'r ' + tone(t.pnl_usd)),
        td(n(rText(t.pnl_r)), 'r ' + tone(t.pnl_r)));
      tbody.append(tr);
    }
    const pendingCount = trades.filter((t) => t.fees_pending).length;
    put(body, wrap, pendingCount ? el('p', 'note', 'Fees pending on ' + pendingCount + ' of ' +
      trades.length + '; their P&L is before fees.') : null);
  }

  function renderPast() {
    const body = byId('past-body');
    const days = [...doc.past.days].reverse();
    if (!days.length) {
      put(body, empty('No closed trades yet.'));
      return;
    }
    const {wrap, body: tbody} = table([['Day', 'l'], ['Market', 'l'], ['Trades', 'r'],
      ['Won', 'r'], ['P&L', 'r'], ['Running total', 'r']], 640, 'days');
    for (const d of days) {
      const tr = el('tr');
      tr.append(td(add(el('span'), dayLabel(d.day, true), d.excluded ? el('span', 'tag',
        'excluded from stats') : null)), td(d.market || DASH, 'muted'),
      td(n(String(d.closed)), 'r'), td(n(String(d.wins)), 'r'),
      td(n(money(d.pnl_usd, true)), 'r ' + tone(d.pnl_usd)),
      td(n(money(d.cumulative_pnl_usd, true)), 'r'));
      tbody.append(tr);
    }
    put(body, wrap);
  }

  // --- A trade's own page -----------------------------------------------------------------------

  function findTrade(number) {
    const open = doc.live_trades.find((t) => t.trade_no === number);
    if (open) return [open, false];
    const closed = doc.past.closed_trades.find((t) => t.trade_no === number);
    return closed ? [closed, true] : [null, false];
  }

  function tradeHeader(t, closed) {
    const head = el('section', 'trade-head');
    const meta = ['#' + t.trade_no, t.tag === 'Jev-managed' ? 'managed by Jev' :
      'fixed exit'];
    if (t.agent) meta.push('picked by ' + t.agent);
    if (t.rules) meta.push(t.rules === 'CURRENT' ? 'current rules' : 'earlier rules');
    if (t.excluded) meta.push('excluded from stats');
    let line;
    if (closed) {
      line = 'Bought ' + etStamp(t.entry_at) + ' ET, sold ' + etStamp(t.exit_at) + ' ET · held ' +
        minutesWords(t.minutes_in_trade) + ' · ' + (t.exit_reason || 'closed');
    } else {
      line = 'Bought ' + etStamp(t.entry_at) + ' ET · open ' + minutesWords(t.minutes_in_trade);
      if (t.closing) line += ' · closing';
    }
    const left = add(el('div', 'trade-title'), n(meta.join(' · '), 'trade-meta'),
      el('h1', null, t.symbol), el('div', 'trade-line', line));
    const sub = [];
    if (num(t.pnl_r) !== null) sub.push(rText(t.pnl_r));
    if (closed) sub.push(t.fees_pending ? 'before fees · fees pending' : 'after fees of ' +
      money(t.fees_usd));
    else if (t.price_live) sub.push('at the live bid');
    else if (t.price_at) sub.push('at the ledger price, ' + ago(t.price_at));
    const right = add(el('div', 'trade-result'), n(money(t.pnl_usd, true), 'huge ' +
      tone(t.pnl_usd)), el('span', 'trade-line', sub.join(' · ')));
    head.append(left, right);
    return head;
  }

  function stopSteps(t, closed, tEnd) {  // [{t, stop, target}] from the entry to the end.
    const steps = (t.levels || []).map((l) => ({t: Date.parse(l.at), stop: num(l.stop),
      target: num(l.target)})).filter((l) => Number.isFinite(l.t));
    if (!steps.length && t.entry_at) {
      steps.push({t: Date.parse(t.entry_at), stop: num(t.planned_stop),
        target: num(t.planned_target)});
    }
    if (!closed && num(t.stop) !== null && steps.length && steps.at(-1).stop !== num(t.stop)) {
      steps.push({t: tEnd, stop: num(t.stop), target: num(t.target)});
    }
    return steps;
  }

  function chartBlock(t, closed) {
    const box = el('section', 'block chart-block');
    const head = add(el('div', 'chart-head'), label('Price'));
    box.append(head);
    const data = chart.trade === t.trade_no ? chart.data : null;
    if (!data) {
      box.append(el('p', 'note', 'Loading the price bars…'));
      return box;
    }
    if (!data.candles || !data.candles.length) {
      box.append(el('p', 'note', data.status === 'UNAVAILABLE' ? 'Price bars are unavailable ' +
        'right now; the levels and the story are below.' : 'No price bars for this window.'));
      return box;
    }
    head.append(el('span', 'chart-sub', {'1Min': '1-minute', '5Min': '5-minute',
      '15Min': '15-minute'}[data.timeframe] + ' candles' + (data.after && data.after.length ?
      ' · then 24 h after the sale' : '') + ' · public market data'));
    const legend = add(el('div', 'legend'),
      add(el('span', 'legend-item'), lineKey(C.loss), 'Stop'),
      add(el('span', 'legend-item'), lineKey(C.gain), 'Target'),
      add(el('span', 'legend-item'), lineKey(C.ink), 'Entry'),
      t.research_stop && num(t.research_stop) !== num(t.planned_stop) ? add(el('span',
        'legend-item'), lineKey(C.research), 'Research stop') : null,
      data.after && data.after.length ? add(el('span', 'legend-item'), lineKey(C.after),
        'After the sale') : null);
    const host = el('div', 'chart chart-candles');
    const volHost = el('div', 'chart chart-volume');
    const controls = el('div', 'chart-controls');
    if (closed && data.after && data.after.length) {
      const row = el('div', 'range-row');
      row.setAttribute('role', 'group');
      row.setAttribute('aria-label', 'Time range');
      for (const [key, words] of [['TRADE', 'The trade'], ['DAY', '+24 h after']]) {
        const b = el('button', 'range' + (ui.tradeRange === key ? ' on' : ''), words);
        b.type = 'button';
        b.setAttribute('aria-pressed', String(ui.tradeRange === key));
        b.addEventListener('click', () => {
          ui.tradeRange = key;
          drawn.delete('trade');
          render();
        });
        row.append(b);
      }
      controls.append(row);
    }
    controls.append(legend);
    box.append(controls);
    box.append(host, volHost, twin('View as table', () => candleTable(data, t)));
    box.draw = () => drawCandles(host, volHost, data, t, closed);
    return box;
  }

  function drawCandles(host, volHost, data, t, closed) {
    const candles = data.candles.map((b) => ({t: Date.parse(b.t), o: num(b.o), h: num(b.h),
      l: num(b.l), c: num(b.c), v: num(b.v)}));
    const after = (data.after || []).map((b) => ({t: Date.parse(b.t), v: num(b.c)}));
    const stepMs = {'1Min': 60000, '5Min': 300000, '15Min': 900000}[data.timeframe] || 300000;
    const entryAt = Date.parse(t.entry_at);
    const exitAt = closed ? Date.parse(t.exit_at) : null;
    let tEnd = Math.max(candles.at(-1).t + stepMs, after.length ? after.at(-1).t + 300000 :
      0, closed ? exitAt : serverNow());
    if (closed && ui.tradeRange !== 'DAY') {  // The trade, plus a little of what came after.
      const held = exitAt - Date.parse(t.entry_at);
      tEnd = Math.min(tEnd, exitAt + Math.min(24 * HOUR, Math.max(2 * HOUR, held)));
    }
    const shownAfter = after.filter((p) => p.t + 300000 <= tEnd);
    const t0 = candles[0].t;
    const steps = stopSteps(t, closed, tEnd);
    const entry = num(t.entry), exit = closed ? num(t.exit) : null;
    const research = num(t.research_stop);
    const levelValues = [entry, exit, research, ...steps.map((st) => st.stop),
      ...steps.map((st) => st.target)].filter((v) => v !== null);
    const lows = candles.map((b) => b.l).concat(shownAfter.map((p) => p.v), levelValues);
    const highs = candles.map((b) => b.h).concat(shownAfter.map((p) => p.v), levelValues);
    const phone = host.clientWidth < 560;
    const margin = {left: phone ? 8 : 12, right: phone ? 122 : 150, top: 12, bottom: 28};
    const {svg, plot} = frame(host, phone ? 260 : 360, margin);
    const lo = Math.min(...lows), hi = Math.max(...highs);
    const pad = (hi - lo) * 0.06 || hi * 0.01;
    const yt = niceTicks(lo - pad, hi + pad, phone ? 4 : 6);
    const y = scale(yt.lo, yt.hi, plot.bottom, plot.top);
    const x = scale(t0, tEnd, plot.left, plot.right);
    // Price ticks on the right edge (the levels' labels sit there too, in the gutter).
    const g = s('g', {class: 'axis'});
    for (const v of yt.ticks) {
      const py = Math.round(y(v)) + 0.5;
      g.append(s('line', {x1: plot.left, x2: plot.right, y1: py, y2: py, stroke: C.grid}));
    }
    svg.append(g);
    // The holding window, faintly shaded.
    const hx0 = x(entryAt), hx1 = x(closed ? exitAt : tEnd);
    svg.append(s('rect', {x: hx0, y: plot.top, width: Math.max(1, hx1 - hx0),
      height: plot.bottom - plot.top, fill: C.account, 'fill-opacity': 0.05}));
    const tt = timeTicks(t0, tEnd, plot.right - plot.left);
    xAxis(svg, plot, x, tt.ticks, tt.step);
    // Levels: the research stop (if different), entry, and the stop and target over time.
    const labels = [];
    if (research !== null && research !== num(t.planned_stop)) {
      svg.append(s('line', {x1: plot.left, x2: plot.right, y1: y(research), y2: y(research),
        stroke: C.research, 'stroke-width': 1}));
      labels.push({v: research, words: 'Research stop ' + price(research), color: C.research});
    }
    if (entry !== null) {
      svg.append(s('line', {x1: plot.left, x2: plot.right, y1: y(entry), y2: y(entry),
        stroke: C.ink, 'stroke-width': 1, opacity: 0.7}));
      labels.push({v: entry, words: 'Entry ' + price(entry), color: C.ink});
    }
    const levelEnd = closed ? exitAt : tEnd;
    for (const [key, color, name] of [['stop', C.loss, 'Stop'], ['target', C.gain, 'Target']]) {
      const pts = steps.filter((st) => st[key] !== null).map((st) => ({t: Math.max(st.t, t0),
        v: st[key]}));
      if (!pts.length) continue;
      pts.push({t: levelEnd, v: pts.at(-1).v});
      // The level faintly across the chart, solid (and stepping) while it was in force.
      svg.append(s('line', {x1: plot.left, x2: plot.right, y1: y(pts.at(-1).v),
        y2: y(pts.at(-1).v), stroke: color, 'stroke-width': 1, opacity: 0.3}));
      svg.append(s('path', {d: linePath(pts, x, y, () => true), fill: 'none', stroke: color,
        'stroke-width': 1}));
      const moved = new Set(pts.map((p) => p.v)).size > 1;
      labels.push({v: pts.at(-1).v, words: (moved ? 'Raised ' + name.toLowerCase() : name ===
        'Stop' && t.plan ? 'Plan stop' : name) + ' ' + price(pts.at(-1).v), color});
    }
    // Candles: thin bodies and 1 px wicks; up outlined green on a light fill, down filled red.
    const bw = Math.max(1, Math.min(8, (x(t0 + stepMs) - x(t0)) * 0.7));
    const cg = s('g');
    for (const b of candles) {
      const px = x(b.t + stepMs / 2);
      const up = b.c >= b.o;
      const color = up ? C.gain : C.loss;
      cg.append(s('line', {x1: px, x2: px, y1: y(b.h), y2: y(b.l), stroke: color,
        'stroke-width': 1}));
      const top = y(Math.max(b.o, b.c)), height = Math.max(1, Math.abs(y(b.o) - y(b.c)));
      cg.append(s('rect', {x: px - bw / 2, y: top, width: bw, height,
        fill: up ? '#E6F2EC' : color, stroke: color, 'stroke-width': bw > 2 ? 1 : 0}));
    }
    svg.append(cg);
    if (shownAfter.length > 1) {
      const line = [{t: exitAt, v: exit !== null ? exit : shownAfter[0].v}, ...shownAfter.map((p) => ({
        t: p.t + 300000, v: p.v}))];
      svg.append(s('path', {d: linePath(line, x, y), fill: 'none', stroke: C.after,
        'stroke-width': 2, 'stroke-linejoin': 'round'}));
    }
    // Entry ▲ and exit ▼, 8 px with a 2 px surface ring.
    const tri = (px, py, upward, color) => {
      const h = 8;
      const d = upward ? 'M' + px + ' ' + (py + 3) + 'l' + (-h / 2 - 1) + ' ' + (h + 1) + 'h' +
        (h + 2) + 'Z' : 'M' + px + ' ' + (py - 3) + 'l' + (-h / 2 - 1) + ' ' + (-h - 1) + 'h' +
        (h + 2) + 'Z';
      svg.append(s('path', {d, fill: color, stroke: '#FFFFFF', 'stroke-width': 2,
        'paint-order': 'stroke'}));
    };
    if (entry !== null) tri(x(entryAt), y(entry), true, C.ink);
    if (closed && exit !== null) tri(x(exitAt), y(exit), false, exit >= entry ? C.gain : C.loss);
    // Labels at the right edge (text tokens; a short line key carries the colour).
    labels.sort((a, b) => y(a.v) - y(b.v));
    let lastY = -Infinity;
    const taken = [];
    for (const lab of labels) {
      const ly = Math.max(y(lab.v), lastY + 15);
      lastY = ly;
      taken.push(ly);
      svg.append(s('line', {x1: plot.right + 4, x2: plot.right + 12, y1: ly, y2: ly,
        stroke: lab.color, 'stroke-width': 2}));
      svg.append(s('text', {x: plot.right + 16, y: ly + 4, class: 'level-label'}, phone ?
        lab.words.replace(/^(Research stop|Raised stop|Raised target|Plan stop) /, (m) =>
          m.split(' ')[0] + ' ') : lab.words));
    }
    // Price ticks in the same gutter, wherever no level label sits.
    const priceTicks = s('g', {class: 'axis'});
    for (const v of yt.ticks) {
      const py = Math.round(y(v)) + 0.5;
      if (py < plot.top - 1 || py > plot.bottom + 1 || taken.some((ly) => Math.abs(ly - py) <
        14)) continue;
      priceTicks.append(s('text', {x: plot.right + 16, y: py + 4, class: 'tick'},
        priceTick(v, yt.step)));
    }
    svg.append(priceTicks);
    // Pacing waits and Jev's confirmations: small ticks on the time axis with tooltips.
    const events = (t.events || []).filter((e) => e.at && (e.kind === 'WAIT' || (e.kind ===
      'REVIEW' && ['CONFIRMING', 'FLAGGED', 'APPLIED'].includes(e.outcome))));
    host.append(svg);
    const tip = tooltip(host);
    for (const e of events) {
      const at = Date.parse(e.kind === 'WAIT' ? e.since || e.at : e.at);
      if (at < t0 || at > tEnd) continue;
      const px = x(at);
      const color = e.kind === 'WAIT' ? C.warn : C.jev;
      svg.append(s('line', {x1: px, x2: px, y1: plot.bottom - 7, y2: plot.bottom, stroke: color,
        'stroke-width': 2}));
      const hit = s('rect', {x: px - 12, y: plot.bottom - 14, width: 24, height: 24,
        fill: 'transparent', tabindex: 0, class: 'hit'});
      const show = () => tip.show(px * (host.clientWidth / svg.viewBox.baseVal.width),
        plot.bottom - 30, [el('div', 'tip-time', etStamp(at) + ' ET'), el('div', 'tip-note',
          (e.kind === 'WAIT' ? 'Entry waited: ' : 'Jev: ') + e.text)]);
      hit.addEventListener('pointerenter', show);
      hit.addEventListener('focus', show);
      hit.addEventListener('pointerleave', tip.hide);
      hit.addEventListener('blur', tip.hide);
      svg.append(hit);
    }
    // The crosshair: time, O/H/L/C and the distance from the entry (% and R).
    const positions = candles.map((b) => ({...b, t: b.t + stepMs / 2}));
    for (const p of shownAfter) positions.push({t: p.t + 300000, c: p.v, after: true});
    positions.sort((a, b) => a.t - b.t);
    const risk = num(t.limit) !== null && num(t.planned_stop) !== null ?
      num(t.limit) - num(t.planned_stop) : null;
    crosshair(svg, plot, host, positions, x, (p, layer) => {
      dot(layer, x(p.t), y(p.c), p.after ? C.after : C.ink, 3);
      const rows = [el('div', 'tip-time', etDay(p.t) + ' ' + etTime(p.t - (p.after ? 300000 :
        stepMs / 2)) + ' ET' + (p.after ? ' · after the sale' : ''))];
      if (!p.after) {
        const grid = el('div', 'tip-grid');
        for (const [name, v] of [['O', p.o], ['H', p.h], ['L', p.l], ['C', p.c]]) {
          grid.append(el('span', 'tip-label', name), n(price(v), 'tip-value'));
        }
        rows.push(grid);
      } else rows.push(tipRow(price(p.c), 'close'));
      if (entry) {
        const change = (p.c - entry) / entry * 100;
        rows.push(tipRow(signedPct(change) + (risk && risk > 0 ? ' · ' + rText((p.c - entry) /
          risk) : ''), 'from the entry', null, tone(change)));
      }
      return rows;
    });
    // Volume: its own small column chart (no second axis on the price chart).
    const vframe = frame(volHost, 64, {...margin, top: 4, bottom: 6});
    // Linear volume, scaled to the 95th percentile so one block print does not flatten the
    // rest; a taller bar is clipped at the top.
    const sorted = candles.map((b) => b.v || 0).sort((a, b) => a - b);
    const p95 = sorted[Math.floor(sorted.length * 0.95)] || 0;
    const vmax = Math.max(p95 * 1.25, sorted.at(-1) <= p95 * 1.25 ? sorted.at(-1) : 0) ||
      sorted.at(-1);
    const printed = sorted.filter((v) => v > 0).length;
    if (printed < candles.length / 4) {
      // Alpaca's own crypto venue prints few trades: a mostly empty volume chart would mislead.
      volHost.append(el('p', 'note', 'Volume not shown: the public feed printed trades in ' +
        printed + ' of ' + candles.length + ' bars.'));
    } else if (vmax > 0) {
      const vy = scale(0, vmax, vframe.plot.bottom, vframe.plot.top);
      const vx = scale(t0, tEnd, vframe.plot.left, vframe.plot.right);
      const vg = s('g');
      for (const b of candles) {
        if (!b.v) continue;
        const px = vx(b.t + stepMs / 2);
        const top = vy(Math.min(b.v, vmax));
        vg.append(s('rect', {x: px - bw / 2, y: top, width: bw,
          height: Math.max(0.5, vframe.plot.bottom - top), fill: C.volume}));
      }
      vframe.svg.append(vg);
      vframe.svg.append(s('text', {x: vframe.plot.right + 16, y: vframe.plot.top + 10,
        class: 'level-label muted-label'}, 'Volume'));
      volHost.append(vframe.svg);
    }
  }

  function candleTable(data, t) {
    const {wrap, body} = table([['Time (ET)', 'l'], ['Open', 'r'], ['High', 'r'], ['Low', 'r'],
      ['Close', 'r'], ['Volume', 'r']], 560, 'data');
    const rows = data.candles.slice().reverse();
    for (const b of rows.slice(0, 300)) {
      const tr = el('tr');
      tr.append(td(n(etDay(b.t) + ' ' + etTime(b.t))), td(n(price(b.o)), 'r'),
        td(n(price(b.h)), 'r'), td(n(price(b.l)), 'r'), td(n(price(b.c)), 'r'),
        td(n(quantity(b.v)), 'r muted'));
      body.append(tr);
    }
    return add(el('div'), wrap, rows.length > 300 ? el('p', 'note', 'The latest 300 of ' +
      rows.length + ' bars.') : null, el('p', 'note', 'Levels: entry ' + price(t.entry) +
      ', stop ' + price(t.planned_stop) + ', target ' + price(t.planned_target) + '.'));
  }

  function levelsBlock(t, closed) {
    const box = el('section', 'block');
    box.append(label('Levels'));
    const initial = t.levels && t.levels.length ? t.levels[0] : null;
    const stop = num(initial ? initial.stop : t.planned_stop);
    const target = num(t.target !== undefined && t.target !== null ? t.target : t.planned_target);
    const entry = num(t.entry);
    const out = closed ? num(t.exit) : num(t.price);
    const raised = (t.levels || []).slice(1).map((st) => [num(st.stop), st.at])
      .filter(([v]) => v !== null && v !== stop);
    const values = [stop, target, entry, out, ...raised.map(([v]) => v)].filter((v) => v !== null);
    if (values.length >= 2 && Math.max(...values) > Math.min(...values)) {
      const lo = Math.min(...values), hi = Math.max(...values);
      const pos = (v) => (v - lo) / (hi - lo) * 100;
      const bar = el('div', 'levels-bar');
      bar.setAttribute('role', 'img');
      bar.setAttribute('aria-label', 'Stop ' + price(stop) + ', entry ' + price(entry) +
        (out !== null ? (closed ? ', exit ' : ', price ') + price(out) : '') + ', target ' +
        price(target));
      bar.append(el('div', 'track'));
      if (entry !== null && out !== null) {
        const fill = el('div', 'fill ' + (out >= entry ? 'pos-bg' : 'neg-bg'));
        fill.style.left = Math.min(pos(entry), pos(out)).toFixed(2) + '%';
        fill.style.width = Math.abs(pos(out) - pos(entry)).toFixed(2) + '%';
        bar.append(fill);
      }
      const marks = [[stop, 'Stop ' + price(stop), 'top', 'm-stop']];
      raised.forEach(([v, at], i) => {
        marks.push([v, i === raised.length - 1 ? 'Raised ' + price(v) + (at ? ' (' +
          etTime(at) + ')' : '') : '', 'bottom', 'm-raised']);
      });
      marks.push([entry, 'Entry ' + price(entry), 'top', 'm-entry']);
      if (out !== null) marks.push([out, (closed ? 'Exit ' : 'Now ') + price(out), 'bottom',
        out >= (entry || 0) ? 'm-exit-pos' : 'm-exit-neg']);
      marks.push([target, 'Target ' + price(target), 'top', 'm-target']);
      // Each label takes the first row (above, below, further below) where it overlaps no
      // other label, measured at the bar's likely width (12 px gutters, 7 px per character).
      const width = Math.max(240, Math.min(1176, window.innerWidth - (window.innerWidth < 640 ?
        66 : 114)));
      const rows = {top: [], bottom: [], third: []};
      let third = false;
      for (const [v, words, preferred, cls] of marks) {
        if (v === null) continue;
        const p = pos(v);
        const tick = el('div', 'tick ' + cls);
        tick.style.left = p.toFixed(2) + '%';
        bar.append(tick);
        if (!words) continue;
        const w = words.length * 7 + 8;
        const anchor = p < 12 ? 'from-left' : p > 88 ? 'from-right' : 'centred';
        const x = p / 100 * width;
        const span = anchor === 'from-left' ? [x, x + w] : anchor === 'from-right' ?
          [x - w, x] : [x - w / 2, x + w / 2];
        const order = preferred === 'top' ? ['top', 'bottom', 'third'] :
          ['bottom', 'top', 'third'];
        const row = order.find((r) => rows[r].every(([a, b]) => span[1] < a || span[0] > b)) ||
          'third';
        rows[row].push(span);
        if (row === 'third') third = true;
        const tag = el('div', 'tick-label ' + row + ' ' + cls, words);
        tag.style.left = p.toFixed(2) + '%';
        tag.classList.add(anchor);
        bar.append(tag);
      }
      if (third) bar.classList.add('three-rows');
      box.append(bar);
    }
    const facts = el('div', 'level-facts');
    const fact = (name, value) => add(el('div', 'fact'), el('span', 'fact-label', name),
      value instanceof Node ? value : n(value));
    const qty = num(t.qty), value = num(t.entry_value_usd);
    facts.append(fact('Size', quantity(qty) + ' ' + base(t.symbol) + (value !== null ? ' · ' +
      money(value) : '')));
    if (entry !== null && stop !== null && entry > 0) {
      facts.append(fact('Risk (1R)', ((entry - stop) / entry * 100).toFixed(1) + '%' +
        (num(t.risk_usd) !== null ? ' · ' + money(t.risk_usd) : '')));
    }
    if (entry !== null && target !== null && stop !== null && entry > stop) {
      facts.append(fact('Target distance', signedPct((target - entry) / entry * 100, 1) +
        ' · ' + ((target - entry) / (entry - stop)).toFixed(1) + 'R'));
    }
    if (t.market_at_entry && t.market_at_entry.words) {
      facts.append(fact('Market at entry', el('span', null, t.market_at_entry.words)));
    }
    box.append(facts);
    return box;
  }

  function planBlock(t) {
    const p = t.plan;
    if (!p) return null;
    const box = el('section', 'block');
    box.append(label('The plan'));
    const {wrap, body} = table([['', 'l'], ['Research', 'r'], ['Traded', 'r'], ['Rule', 'l']],
      600, 'plan');
    const rows = [
      ['Stop', price(p.research_stop), price(p.stop), p.stop_rule],
      ['Target', price(p.research_target), price(p.target), p.target_rule],
      ['Size', DASH, quantity(t.qty) + ' ' + base(t.symbol), num(p.risk_usd) !== null ?
        'Sized to the risk per trade: ' + money(p.risk_usd) + ' to the stop' : 'Sized to the ' +
        'risk per trade'],
    ];
    if (p.window_hours) rows.push(['Window', DASH, p.window_hours + ' h',
      'Reviewed at the end of the window']);
    for (const [name, research, traded, rule] of rows) {
      const tr = el('tr');
      tr.append(td(name), td(n(research), 'r muted'), td(n(traded), 'r'), td(rule, 'muted'));
      body.append(tr);
    }
    box.append(wrap);
    return box;
  }

  function who(e, t) {
    if (e.kind === 'EXIT' && e.reason === 'Daily loss halt') return WHO.LIMIT;
    if (e.actor === 'RESEARCH_AGENT') return [e.agent || t.agent || 'Agent', 'who-agent'];
    return WHO[e.actor] || WHO.APP;
  }

  function eventTime(e) {
    if (e.kind === 'WAIT') return span(e.since, e.until);
    if (e.repeats > 1 && e.since) return span(e.since, e.at);
    return e.at ? etStamp(e.at) : DASH;
  }

  function eventNote(e) {
    const parts = [];
    if (e.note) parts.push(e.note);
    if (e.kind === 'REVIEW' && e.outcome === 'APPLIED' && num(e.confidence) !== null) {
      parts.push('confidence ' + num(e.confidence).toFixed(2));
    }
    if (e.kind === 'BUY' && num(e.value_usd) !== null) parts.push(money(e.value_usd));
    return parts.join(' · ');
  }

  function timelineBlock(t) {
    const box = el('section', 'block-plain');
    box.append(el('h2', null, 'What happened'));
    const list = el('ol', 'timeline');
    for (const e of t.events || []) {
      if (e.kind === 'GAP') {
        list.append(add(el('li', 'gap'), el('span', 'muted', e.text)));
        continue;
      }
      const [name, cls] = who(e, t);
      const note = eventNote(e);
      list.append(add(el('li'), n(eventTime(e), 'when'), el('span', 'who ' + cls, name),
        add(el('div', 'what'), el('span', 'what-text', e.text), note ? el('span', 'what-note',
          note) : null)));
    }
    box.append(list);
    return box;
  }

  function afterBlock(t) {  // From the public bars after the exit; omitted without them.
    const a = (chart.trade === t.trade_no && chart.data && chart.data.after_exit) ||
      t.after_exit;
    if (!a || !Array.isArray(a.points) || !a.points.length) return null;
    const box = el('section', 'block-plain');
    box.append(add(el('div', 'chart-head'), el('h2', null, 'After the sale'),
      el('span', 'chart-sub', 'sold at ' + price(a.exit_price))));
    const grid = el('div', 'figures after-grid');
    for (const p of a.points) {
      const cell = add(el('div', 'figure'), el('span', 'figure-label', p.label));
      if (p.pending) {
        cell.append(add(el('span', 'figure-sub'), 'due ' + etStamp(p.at) + ' ET ', pending()));
      } else {
        cell.append(n(signedPct(p.change_pct), 'mid ' + tone(p.change_pct)),
          el('span', 'figure-sub', price(p.price) + (num(p.r) !== null ? ' · ' + rText(p.r) +
            ' vs the exit' : '')));
      }
      grid.append(cell);
    }
    box.append(grid);
    return box;
  }

  async function loadChart(number, closed) {
    if (chart.loading) return;
    const due = chart.trade !== number || !chart.data ||
      Date.now() - chart.at > (closed ? 5 * CHART_REFRESH_MS : CHART_REFRESH_MS);
    if (!due) return;
    chart.loading = true;
    try {
      const response = await fetch('/api/public/experiment/trades/' + number + '/chart', {
        cache: 'no-store', headers: {Accept: 'application/json'}});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      chart.data = await response.json();
      chart.trade = number;
    } catch (error) {
      if (chart.trade !== number) {
        chart.data = {candles: [], status: 'UNAVAILABLE'};
        chart.trade = number;
      }
    }
    chart.at = Date.now();
    chart.loading = false;
    drawn.delete('trade');
    render();
  }

  function renderTrade() {
    const number = Number(document.body.dataset.trade);
    const body = byId('trade-body');
    const [t, closed] = findTrade(number);
    const crumb = byId('crumb');
    if (!t) {
      crumb.textContent = '';
      put(body, empty('Trade #' + number + ' is no longer listed.'));
      return;
    }
    crumb.textContent = (closed ? 'Closed trades · ' + etDay(t.exit_at, true) : 'Open positions');
    document.title = base(t.symbol) + ' #' + t.trade_no + ' · ' + doc.title;
    const priceBox = chartBlock(t, closed);
    put(body, tradeHeader(t, closed), priceBox, afterBlock(t), levelsBlock(t, closed),
      planBlock(t), timelineBlock(t));
    if (priceBox.draw) priceBox.draw();  // Drawn in place: the chart takes its box's width.
    loadChart(number, closed);
  }

  // --- Rendering and polling --------------------------------------------------------------------

  const MAIN = {
    status: [() => [doc.system], renderStatus],
    account: [() => [doc.account, doc.open_risk], renderAccount],
    equity: [() => [doc.equity, ui.range, ui.width], renderEquity],
    performance: [() => [doc.performance, doc.daily, ui.width], renderPerformance],
    open: [() => [doc.live_trades, Math.floor(serverNow() / 60000)], renderOpen],
    tiles: [() => [doc.limits, doc.market], renderTiles],
    agents: [() => [doc.agents, doc.feed, Math.floor(serverNow() / 60000)], renderAgents],
    closed: [() => [doc.past.closed_trades, doc.as_of.slice(0, 10)], renderClosed],
    past: [() => [doc.past.days], renderPast],
  };
  const TRADE = {
    status: MAIN.status,
    trade: [() => [doc.live_trades, doc.past.closed_trades, chart.at, ui.width, ui.tradeRange,
      Math.floor(serverNow() / 60000)], renderTrade],
  };

  function render() {
    renderHeader();
    for (const [name, [inputs, draw]] of Object.entries(view() === 'trade' ? TRADE : MAIN)) {
      if (!byId(name === 'status' ? 'status-body' : name === 'trade' ? 'trade-body' :
        name + '-body')) continue;
      const now = JSON.stringify(inputs());
      if (drawn.get(name) === now) continue;  // Same figures: not redrawn.
      drawn.set(name, now);
      draw();
    }
    document.body.dataset.ready = '1';
  }

  async function poll() {
    if (document.hidden || polling) return;  // A background tab waits; it refreshes when shown.
    polling = true;
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), POLL_TIMEOUT_MS);
    const dim = setTimeout(() => document.body.classList.add('refreshing'), DIM_AFTER_MS);
    try {
      const response = await fetch('/api/public/experiment', {cache: 'no-store',
        headers: {Accept: 'application/json'}, signal: abort.signal});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      doc = await response.json();
      receivedAt = Date.now();
      failures = 0;
      render();
    } catch (error) {
      failures += 1;
      renderHeader();
    } finally {
      clearTimeout(timer);
      clearTimeout(dim);
      document.body.classList.remove('refreshing');
      polling = false;
    }
  }

  function watchWidth() {  // Charts follow their container; redrawn after a resize settles.
    let timer = null;
    const measure = () => {
      const width = byId('app').clientWidth;
      if (width !== ui.width) {
        ui.width = width;
        if (doc) render();
      }
    };
    ui.width = byId('app').clientWidth;
    window.addEventListener('resize', () => {
      clearTimeout(timer);
      timer = setTimeout(measure, 150);
    });
  }

  function start() {
    const initial = byId('initial-data');
    doc = JSON.parse(initial.textContent);
    receivedAt = Date.now();
    watchWidth();
    render();
    setInterval(poll, POLL_MS);
    setInterval(() => {
      if (doc) renderHeader();
    }, 1000);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) poll();
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
