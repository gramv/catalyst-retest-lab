'use strict';
// Public live dashboard (package experiment-page). Renders the JSON document with DOM calls
// only (textContent; never innerHTML) and polls /api/public/experiment every five seconds.
// An opened trade's price chart and the current cycle's latest prices come from public crypto
// market data that the reader's browser reads at MARKET_DATA, with no key and no credentials:
// the page's Content-Security-Policy allows that one origin besides its own. No third-party
// code.
(function () {
  const POLL_MS = 5000;
  const MARKET_DATA = 'https://data.alpaca.markets/v1beta3/crypto/us';
  const BARS_REFRESH_MS = 60000;  // An open trade's bars are read again after a minute.
  const LATEST_REFRESH_MS = 60000;  // The cycle's latest prices, while its table is on screen.
  const MINUTE = 60000;
  const HOUR = 60 * MINUTE;
  const DAY = 24 * HOUR;
  const MINUS = '−';
  const DASH = '—';
  const SVG = 'http://www.w3.org/2000/svg';
  const US = 'en-US';
  const CLOSED_SHOWN = 8;  // Closed trades listed before "Show all" (5 on a phone).
  const PHONE = '(max-width: 640px)';
  const STALE_PRICE_SECONDS = 300;
  let doc = null;
  let receivedAt = Date.now();
  let failures = 0;
  let phone = window.matchMedia(PHONE).matches;
  const drawn = new Map();  // Section -> the data it was last drawn from (unchanged: not redrawn).
  const expanded = new Set();  // Opened trades ('open:12', 'closed:7'), kept across refreshes.
  const opened = new Map();  // Collapsible lists and "Show all": the reader's choice.
  const lastPrice = new Map();  // Trade number -> the price last drawn (a change flashes).
  const bars = new Map();  // Trade key -> its price history, as read from MARKET_DATA.
  // The current cycle's latest public prices: symbol -> {price, at}; read only while the
  // cycle's table is on screen (``visible``) and the tab is shown.
  const latest = {prices: new Map(), at: 0, symbols: '', loading: false, visible: false,
    version: 0};
  let jevScope = 'cycle';  // Jev's log: 'cycle' (this cycle) or 'today'.

  const byId = (id) => document.getElementById(id);

  function el(tag, cls, words) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (words !== undefined && words !== null) node.textContent = String(words);
    return node;
  }

  function svg(tag, attributes) {
    const node = document.createElementNS(SVG, tag);
    for (const [name, value] of Object.entries(attributes || {})) {
      node.setAttribute(name, String(value));
    }
    return node;
  }

  function add(parent, ...children) {
    for (const child of children) {
      if (child === null || child === undefined || child === false) continue;
      parent.append(typeof child === 'string' ? document.createTextNode(child) : child);
    }
    return parent;
  }

  function put(parent, ...children) {  // replaceChildren, skipping absent parts (never "null").
    parent.replaceChildren();
    return add(parent, ...children);
  }

  function text(value) {
    return document.createTextNode(value);
  }

  function n(words, cls) {  // A figure in the monospace face.
    return el('span', 'n' + (cls ? ' ' + cls : ''), words);
  }

  // --- Numbers ------------------------------------------------------------------------------

  function num(value) {
    if (value === null || value === undefined || value === '') return null;
    const x = Number(value);
    return Number.isFinite(x) ? x : null;
  }

  function money(value, signed, digits) {
    const x = num(value);
    if (x === null) return DASH;
    const places = digits === undefined ? 2 : digits;
    const abs = Math.abs(x).toLocaleString(US, {minimumFractionDigits: places,
      maximumFractionDigits: places});
    if (!signed) return (x < 0 ? MINUS : '') + '$' + abs;
    if (Math.abs(x) < 0.005) return '$0.00';
    return (x > 0 ? '+' : MINUS) + '$' + abs;
  }

  function rText(value) {
    const x = num(value);
    if (x === null) return DASH;
    if (Math.abs(x) < 0.005) return '0.00R';
    return (x > 0 ? '+' : MINUS) + Math.abs(x).toFixed(2) + 'R';
  }

  function tone(value) {
    const x = num(value);
    if (x === null || Math.abs(x) < 0.005) return '';
    return x > 0 ? 'pos' : 'neg';
  }

  function pct(value) {
    const x = num(value);
    return x === null ? DASH : Math.round(x * 100) + '%';
  }

  function change(from, to) {  // Percent change, or null.
    const a = num(from);
    const b = num(to);
    return a === null || b === null || a === 0 ? null : (b - a) / a * 100;
  }

  function signedPct(p) {
    if (p === null) return DASH;
    if (Math.abs(p) < 0.005) return '0.00%';
    return (p > 0 ? '+' : MINUS) + Math.abs(p).toFixed(Math.abs(p) >= 10 ? 1 : 2) + '%';
  }

  function price(value) {
    const x = num(value);
    if (x === null) return DASH;
    if (x >= 1000) return x.toLocaleString(US, {maximumFractionDigits: 2});
    if (x >= 1) return x.toLocaleString(US, {maximumFractionDigits: 4});
    return x.toLocaleString(US, {maximumSignificantDigits: 6});
  }

  function quantity(value) {
    const x = num(value);
    if (x === null) return DASH;
    if (x >= 100000) return x.toLocaleString(US, {maximumFractionDigits: 0});
    if (x >= 1000) return x.toLocaleString(US, {maximumFractionDigits: 2});
    return x.toLocaleString(US, {maximumSignificantDigits: 6});
  }

  function pair(symbol) {  // ['UNI', '/USD']
    const [base, quote] = String(symbol || '?').split('/');
    return [base || '?', quote ? '/' + quote : ''];
  }

  function plural(count, one, many) {
    return count + ' ' + (count === 1 ? one : many);
  }

  // --- Time ---------------------------------------------------------------------------------

  function serverNow() {
    return Date.parse(doc.served_at || doc.as_of) + (Date.now() - receivedAt);
  }

  function span(seconds) {
    if (seconds < 60) return Math.max(1, Math.round(seconds)) + 's';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + 'm';
    const hours = Math.floor(minutes / 60);
    if (hours < 48) return hours + 'h';
    return Math.floor(hours / 24) + 'd';
  }

  function duration(minutes) {
    if (minutes === null || minutes === undefined || minutes < 0) return DASH;
    if (minutes < 60) return minutes + 'm';
    const hours = Math.floor(minutes / 60);
    if (hours < 48) return hours + 'h ' + (minutes % 60) + 'm';
    return Math.floor(hours / 24) + 'd ' + (hours % 24) + 'h';
  }

  function relative(iso, mode) {
    const seconds = (serverNow() - Date.parse(iso)) / 1000;
    if (mode === 'until') return -seconds <= 0 ? 'due now' : 'in ' + span(-seconds);
    if (mode === 'since') return duration(Math.max(0, Math.floor(seconds / 60)));
    return seconds < 2 ? 'just now' : span(seconds) + ' ago';
  }

  // A <time> whose words ("3m ago", "in 20h", "1h 11m") refresh every second.
  function rel(iso, mode, staleAfter) {
    if (!iso) return text(DASH);
    const node = el('time', 'rel');
    node.dateTime = iso;
    node.dataset.rel = mode || 'ago';
    if (staleAfter) node.dataset.staleAfter = String(staleAfter);
    node.title = stamp(iso);
    return refreshTime(node);
  }

  function refreshTime(node) {
    const words = relative(node.dateTime, node.dataset.rel);
    if (node.textContent !== words) node.textContent = words;
    if (node.dataset.staleAfter) {
      const age = (serverNow() - Date.parse(node.dateTime)) / 1000;
      node.classList.toggle('stale', age > Number(node.dataset.staleAfter));
    }
    return node;
  }

  function hhmm(value) {  // 24-hour local time: "14:05".
    return new Date(value).toLocaleTimeString(US, {hour: '2-digit', minute: '2-digit',
      hourCycle: 'h23'});
  }

  function dayShort(value) {
    return new Date(value).toLocaleDateString(US, {month: 'short', day: 'numeric'});
  }

  function stamp(value) {
    return value === null || value === undefined ? DASH : dayShort(value) + ' ' + hhmm(value);
  }

  function shortStamp(value) {  // The time alone when it is today, else with the date.
    if (!value) return DASH;
    const today = new Date(serverNow()).toDateString() === new Date(value).toDateString();
    return today ? hhmm(value) : stamp(value);
  }

  function dayLabel(isoDay, weekday) {  // A calendar day (YYYY-MM-DD), as written.
    return new Date(isoDay.slice(0, 10) + 'T12:00:00Z').toLocaleDateString(US, {
      weekday: weekday ? 'short' : undefined, month: 'short', day: 'numeric', timeZone: 'UTC'});
  }

  function zoneCity(zone) {
    return String(zone || '').split('/').pop().replace(/_/g, ' ');
  }

  // --- Small pieces -------------------------------------------------------------------------

  function srOnly(words) {
    return el('span', 'sr-only', words);
  }

  function empty(message) {
    return el('p', 'empty', message);
  }

  function lines(first, second) {  // A value with a smaller line under it.
    const box = document.createDocumentFragment();
    box.append(first instanceof Node ? first : el('span', 'l1', first));
    if (second !== null && second !== undefined && second !== '') {
      box.append(second instanceof Node ? second : el('span', 'l2', second));
    }
    return box;
  }

  function line(cls, ...children) {
    return add(el('span', cls), ...children);
  }

  function mark(kind) {  // The small square before a decision; its words carry the meaning.
    const dot = el('span', 'mark ' + (kind || 'hold'));
    dot.setAttribute('aria-hidden', 'true');
    return dot;
  }

  const TONES = {APPLIED: 'change', HELD: 'hold', CONTINUE: 'hold', FAILED: 'fail',
    DISCARDED: 'fail', REFUSED: 'fail', FLAGGED: 'flag', EXIT: 'flag'};

  function confidence(value) {
    if (num(value) === null) return null;
    return add(el('span', 'conf'), srOnly('confidence '), text(pct(value)));
  }

  function chevron() {
    const icon = svg('svg', {class: 'chev', viewBox: '0 0 10 10', 'aria-hidden': 'true'});
    icon.append(svg('path', {d: 'M3.2 1.5 6.8 5 3.2 8.5'}));
    return icon;
  }

  function facts(items) {  // Label over value, in a grid of hairline cells.
    const dl = el('dl', 'facts');
    for (const [label, ...value] of items) {
      dl.append(add(el('div'), el('dt', null, label), add(el('dd'), ...value)));
    }
    return dl;
  }

  // --- Masthead -----------------------------------------------------------------------------

  function renderHeader() {
    const state = doc.status.pill;
    byId('title').textContent = doc.title;
    byId('pill').className = 'state ' + String(state).toLowerCase();
    byId('pill-text').textContent = {RUNNING: 'Running', HALTED: 'Halted',
      STOPPED: 'Stopped'}[state] || String(state);
    const o = doc.overall;
    const known = num(o.equity_usd) !== null;
    put(byId('account'), el('span', 'lbl', o.account_label),
      el('span', 'val', known ? money(o.equity_usd) : DASH),
      known && o.equity_at ? add(el('span', 'age'), rel(o.equity_at)) : null);
    renderUpdated();
  }

  function renderUpdated() {
    if (!doc) return;
    const age = Math.max(0, (serverNow() - Date.parse(doc.as_of)) / 1000);
    const lost = failures >= 3;
    let message = age < 2 ? 'updated just now' : 'updated ' + span(age) + ' ago';
    if (lost) message += ' · connection lost, retrying';
    else if (doc.stale) message += ' · stale';
    const node = byId('updated');
    if (node.textContent !== message) node.textContent = message;
    node.classList.toggle('warn', lost || Boolean(doc.stale));
    document.body.classList.toggle('offline', lost);
  }

  // --- Summary strip ------------------------------------------------------------------------

  function figure(label, value, valueCls, subs) {
    const box = add(el('div', 'fig'), el('div', 'fig-label', label));
    box.append(value instanceof Node ? add(el('div', 'fig-value ' + (valueCls || '')), value)
      : el('div', 'fig-value ' + (valueCls || ''), value));
    for (const sub of subs || []) {
      if (sub) box.append(add(el('div', 'fig-sub'), ...[].concat(sub)));
    }
    return box;
  }

  function feesPending(count) {
    return count ? el('span', 'warn', 'fees pending on ' + count) : null;
  }

  function wonLost(wins, losses) {
    return n(wins + 'W–' + losses + 'L');
  }

  function renderSummary() {
    const o = doc.overall;
    const t = doc.today;
    const a = doc.agents;
    const since = doc.status.started_at ? ' · since ' + dayShort(doc.status.started_at) : '';
    const last = a.latest_run ? a.latest_run.run_at : null;
    const next = a.schedule ? a.schedule.next_run_at : null;
    const runs = add(document.createDocumentFragment(), text(last ? hhmm(last) : DASH),
      el('span', 'arrow', '→'), text(next ? hhmm(next) : DASH));
    byId('summary-body').replaceChildren(add(el('div', 'strip'),
      figure('Total P&L' + since, money(o.pnl_usd, true), tone(o.pnl_usd), [
        [n(rText(o.pnl_r), tone(o.pnl_r)), ' · ' + o.closed + ' closed'],
        feesPending(o.fees_pending)]),
      figure('Today' + (t.day ? ' · ' + dayLabel(t.day, true) : ''), money(t.pnl_usd, true),
        tone(t.pnl_usd), [
          [n(rText(t.pnl_r), tone(t.pnl_r)), ' · ', wonLost(t.wins, t.losses),
            num(t.win_rate) !== null ? ' · ' + pct(t.win_rate) : null],
          t.closed + ' closed · ' + t.opened + ' opened', feesPending(t.fees_pending)]),
      figure('Open', o.open ? money(o.open_pnl_usd, true) : DASH, tone(o.open_pnl_usd), [
        o.open ? plural(o.open, 'position', 'positions') : 'No open positions',
        o.open ? [n(money(o.open_entry_value_usd)), ' invested'] : null]),
      figure('Win rate', pct(o.win_rate), '', [[wonLost(o.wins, o.losses)]]),
      figure('Trades', String(o.closed + o.open), '', [
        o.closed + ' closed · ' + o.open + ' open']),
      figure('Research · last → next', runs, '', [
        a.schedule ? a.schedule.text + (a.schedule.times.length === 1 ? ' ' +
          zoneCity(a.schedule.timezone) : '') : 'No schedule',
        [n(String(t.picks)), ' picks · ', n(String(t.selected)), ' selected today']])));
  }

  // --- Trade tables and their opened details ------------------------------------------------

  function table(columns, cls, caption) {
    const t = el('table', 'grid ' + cls);
    if (caption) t.append(add(el('caption', 'sr-only'), text(caption)));
    const head = el('tr');
    for (const column of columns) {
      const th = el('th', 'c-' + column.key + (column.num ? ' num' : ''), column.label);
      th.scope = 'col';
      head.append(th);
    }
    const body = el('tbody');
    t.append(add(el('thead'), head), body);
    return [t, body];
  }

  function cells(tr, columns, content) {
    for (const column of columns) {
      const td = el('td', 'c-' + column.key + (column.num ? ' num' : ''));
      td.dataset.label = column.label;
      const value = content[column.key];
      if (value instanceof Node) td.append(value);
      else td.textContent = value ?? DASH;
      tr.append(td);
    }
    return tr;
  }

  function tradeRows(kind, t, columns, content) {
    const key = kind + ':' + t.trade_no;
    const isOpen = expanded.has(key);
    const row = cells(el('tr', 'trade' + (isOpen ? ' open' : '')), columns, content);
    row.dataset.key = key;
    row.addEventListener('click', (event) => {
      if (event.target.closest('button, a, summary')) return;
      if (String(window.getSelection ? window.getSelection() : '').length) return;
      toggle(key);
    });
    const detail = el('tr', 'detail-row');
    detail.id = 'detail-' + kind + '-' + t.trade_no;
    const td = el('td');
    td.colSpan = columns.length;
    detail.append(td);
    if (isOpen) td.append(detailPanel(kind, t));
    else detail.hidden = true;
    return [row, detail];
  }

  function coinCell(kind, t, ...meta) {
    const key = kind + ':' + t.trade_no;
    const [base, quote] = pair(t.symbol);
    const button = el('button', 'expander');
    button.type = 'button';
    button.setAttribute('aria-expanded', String(expanded.has(key)));
    button.setAttribute('aria-controls', 'detail-' + kind + '-' + t.trade_no);
    button.dataset.focus = 'x:' + key;
    button.append(chevron(), add(el('span'), text(base), el('span', 'quote', quote)),
      srOnly(', trade ' + t.trade_no + ', details'));
    button.addEventListener('click', () => toggle(key));
    return add(document.createDocumentFragment(), button,
      add(el('span', 'l2'), ...meta.filter(Boolean)));
  }

  function toggle(key) {
    if (expanded.has(key)) expanded.delete(key);
    else expanded.add(key);
    const section = key.startsWith('open:') ? 'live' : 'closed';
    drawn.delete(section);
    RENDER[section]();
    const again = document.querySelector('[data-focus="x:' + key + '"]');
    if (again) again.focus({preventScroll: true});
  }

  function tagWords(t) {
    return (t.tag || DASH) + ' · #' + t.trade_no;
  }

  // --- Positions ----------------------------------------------------------------------------

  const LIVE_COLUMNS = [
    {key: 'coin', label: 'Coin'}, {key: 'size', label: 'Size', num: true},
    {key: 'entry', label: 'Entry', num: true}, {key: 'last', label: 'Last', num: true},
    {key: 'stop', label: 'Stop', num: true}, {key: 'target', label: 'Target', num: true},
    {key: 'pnl', label: 'P&L', num: true}, {key: 'time', label: 'In trade', num: true},
    {key: 'review', label: 'Next review', num: true}, {key: 'jev', label: 'Jev'},
  ];

  function lastCell(t) {  // The freshest ledger price, its age, and a flash when it moved.
    const now = el('span', 'l1', price(t.price));
    const before = lastPrice.get(t.trade_no);
    if (before !== undefined && num(before) !== null && num(t.price) !== null &&
        num(before) !== num(t.price)) {
      now.className = 'l1 ' + (num(t.price) > num(before) ? 'tick-up' : 'tick-down');
    }
    lastPrice.set(t.trade_no, t.price);
    if (num(t.price) === null) return lines(now, 'no price yet');
    return lines(now, line('l2', rel(t.price_at, 'ago', STALE_PRICE_SECONDS)));
  }

  function levelCell(level, from) {  // A level and how far the price is from it.
    return lines(price(level), signedPct(change(from, level)));
  }

  function pnlCell(value, second) {
    return lines(el('span', 'l1 ' + tone(value), money(value, true)), second);
  }

  function jevCell(t) {
    const last = t.jev_last;
    const lastChange = t.jev_last_change;
    if (!last && !lastChange) {
      return el('span', 'muted', t.tag === 'Fixed exit' ? 'Fixed stop and target'
        : 'No review yet');
    }
    const box = document.createDocumentFragment();
    if (last) {
      box.append(add(el('span', 'l1'), mark(TONES[last.outcome]), text(last.text),
        confidence(last.confidence)));
    }
    const second = add(el('span', 'l2'), last ? rel(last.at) : null);
    if (lastChange) {
      second.append(text((last ? ' · ' : '') + 'last change: ' + lastChange.text + ', '),
        rel(lastChange.at));
    }
    box.append(second);
    return box;
  }

  function renderLive() {
    const trades = doc.live_trades;
    byId('live-meta').textContent = trades.length ? String(trades.length) + ' open' : '';
    const body = byId('live-body');
    if (!trades.length) {
      body.replaceChildren(empty('No open positions right now.'));
      return;
    }
    const [grid, tbody] = table(LIVE_COLUMNS, 'stack positions', 'Open positions');
    for (const t of trades) {
      const from = num(t.price) === null ? t.entry : t.price;
      const moved = change(t.entry, t.price);
      tbody.append(...tradeRows('open', t, LIVE_COLUMNS, {
        coin: coinCell('open', t, text(tagWords(t)),
          t.closing ? el('span', 'closing', ' · closing') : null),
        size: lines(add(el('span', 'l1'), text(quantity(t.qty)),
          el('span', 'unit', pair(t.symbol)[0])), money(t.entry_value_usd)),
        entry: lines(price(t.entry), t.entry_at ? shortStamp(t.entry_at) : null),
        last: lastCell(t),
        stop: levelCell(t.stop, from),
        target: levelCell(t.target, from),
        pnl: pnlCell(t.pnl_usd, add(el('span', 'l2'), n(signedPct(moved)), text(' · '),
          n(rText(t.pnl_r), tone(t.pnl_r)))),
        time: add(el('span', 'l1'), rel(t.entry_at, 'since')),
        review: t.next_review_at ? add(el('span', 'l1'), rel(t.next_review_at, 'until'))
          : el('span', 'l1 muted', DASH),
        jev: jevCell(t),
      }));
    }
    body.replaceChildren(grid);
    paintCharts(body);
  }

  // --- Closed trades ------------------------------------------------------------------------

  const CLOSED_COLUMNS = [
    {key: 'coin', label: 'Coin'}, {key: 'size', label: 'Size', num: true},
    {key: 'prices', label: 'Entry → exit', num: true}, {key: 'reason', label: 'Exit'},
    {key: 'held', label: 'Held', num: true}, {key: 'fees', label: 'Fees', num: true},
    {key: 'pnl', label: 'P&L', num: true}, {key: 'total', label: 'Running total', num: true},
    {key: 'closed', label: 'Closed', num: true},
  ];

  function renderClosed() {
    const trades = doc.past.closed_trades;
    const total = doc.overall.closed;
    byId('closed-meta').textContent = total ? String(total) : '';
    const body = byId('closed-body');
    if (!trades.length) {
      body.replaceChildren(empty('No closed trades yet.'));
      return;
    }
    const all = opened.get('closed:all') === true;
    const limit = phone ? 5 : CLOSED_SHOWN;
    const [grid, tbody] = table(CLOSED_COLUMNS, 'stack closed', 'Closed trades');
    for (const t of all ? trades : trades.slice(0, limit)) {
      tbody.append(...tradeRows('closed', t, CLOSED_COLUMNS, {
        coin: coinCell('closed', t, text(tagWords(t))),
        size: lines(add(el('span', 'l1'), text(quantity(t.qty)),
          el('span', 'unit', pair(t.symbol)[0])), money(t.entry_value_usd)),
        prices: lines(add(el('span', 'l1'), text(price(t.entry) + ' → '),
          srOnly('exit '), text(price(t.exit))), signedPct(change(t.entry, t.exit))),
        reason: t.exit_reason || DASH,
        held: duration(t.minutes_in_trade),
        fees: t.fees_pending ? el('span', 'l1 warn', 'pending') : money(t.fees_usd),
        pnl: pnlCell(t.pnl_usd, el('span', 'l2 ' + tone(t.pnl_r), rText(t.pnl_r))),
        total: el('span', 'l1 ' + tone(t.cumulative_pnl_usd), money(t.cumulative_pnl_usd,
          true)),
        closed: lines(shortStamp(t.exit_at), add(el('span', 'l2'), rel(t.exit_at))),
      }));
    }
    const parts = [grid];
    if (trades.length > limit) {
      const more = el('button', 'more-btn', all ? 'Show fewer'
        : 'Show ' + (trades.length === total ? 'all ' : 'latest ') + trades.length);
      more.type = 'button';
      more.dataset.focus = 'closed-more';
      more.setAttribute('aria-expanded', String(all));
      more.addEventListener('click', () => {
        opened.set('closed:all', !all);
        renderClosed();
        const again = byId('closed-body').querySelector('[data-focus="closed-more"]');
        if (again) again.focus();
      });
      parts.push(more);
    }
    body.replaceChildren(...parts);
    paintCharts(body);
  }

  // --- A trade's detail: facts, the price chart and the timeline ----------------------------

  function tradeByKey(key) {
    const [kind, number] = key.split(':');
    const list = kind === 'open' ? doc.live_trades : doc.past.closed_trades;
    return list.find((t) => String(t.trade_no) === number) || null;
  }

  function fromTo(before, after, format) {  // "8.52 → 8.79", or one value when unchanged.
    const a = num(before);
    const b = num(after);
    if (a === null || b === null || a === b) return n(format(after === null ? before : after));
    return n(format(before) + ' → ' + format(after));
  }

  function factsLine(kind, t) {
    const item = (label, value) => add(el('span'), el('span', 'k', label), value);
    const box = el('div', 'facts-line');
    if (kind === 'open') {
      box.append(item('Value', fromTo(t.entry_value_usd, t.value_usd, (v) => money(v))));
    } else {
      box.append(item('Value at entry', n(money(t.entry_value_usd))));
    }
    box.append(item('Limit', n(price(t.limit))),
      item('Stop', fromTo(t.planned_stop, t.stop, price)),
      item('Target', fromTo(t.planned_target, t.target, price)));
    if (kind === 'closed') box.append(item('Held', n(duration(t.minutes_in_trade))));
    return box;
  }

  function legend(t) {
    const swatch = (cls, shape) => {
      const icon = svg('svg', {class: 'price', width: 18, height: 10, 'aria-hidden': 'true'});
      if (shape === 'up') icon.append(svg('path', {class: 'buy', d: 'M9 1 14 9 4 9Z'}));
      else if (shape === 'down') icon.append(svg('path', {class: 'sell', d: 'M4 1 14 1 9 9Z'}));
      else if (shape === 'dot') icon.append(svg('circle', {class: 'lastp', cx: 9, cy: 5, r: 3.5}));
      else if (shape === 'rug') {
        icon.append(svg('rect', {class: 'rv', x: 1, y: 3, width: 6, height: 5}),
          svg('rect', {class: 'rv change', x: 10, y: 1, width: 3, height: 8}));
      } else icon.append(svg('line', {class: cls, x1: 0, x2: 18, y1: 5, y2: 5}));
      return icon;
    };
    const item = (icon, words) => add(el('span'), icon, text(words));
    const box = add(el('div', 'legend'), item(swatch('line'), 'Price'),
      item(swatch('entry-line'), 'Entry'), item(swatch('stop'), 'Stop'),
      item(swatch('target'), 'Target'), item(swatch(null, 'up'), 'Buy'));
    if (t.exit_at) box.append(item(swatch(null, 'down'), 'Sell'));
    else if (num(t.price) !== null) box.append(item(swatch(null, 'dot'), 'Last'));
    if (t.tag !== 'Fixed exit') box.append(item(swatch(null, 'rug'), 'Jev reviews'));
    box.setAttribute('aria-hidden', 'true');
    return box;
  }

  function detailPanel(kind, t) {
    const key = kind + ':' + t.trade_no;
    const chart = el('div', 'chart');
    chart.dataset.chart = key;
    const note = el('div', 'chart-note');
    note.dataset.note = key;
    note.setAttribute('aria-live', 'polite');
    const left = add(el('div', 'detail-main'), factsLine(kind, t), el('h3', null, 'Price'),
      legend(t), chart, note);
    const right = add(el('div', 'detail-side'), el('h3', null, 'Timeline'), timeline(kind, t));
    return add(el('div', 'detail'), left, right);
  }

  const EVENT_MARKS = {BUY: 'buy', EXIT: 'sell', LEVELS_SET: 'hold', PICK: 'hold',
    SELECTION: 'change', REVIEW_ANSWER: 'hold', FLAG_ANSWER: 'hold', EXIT_FLAG: 'flag',
    GAP: 'gap'};

  function eventMark(e) {
    if (e.kind === 'RESULT') return tone(e.pnl_usd) === 'neg' ? 'loss' : 'gain';
    if (e.kind === 'REVIEW' || e.kind === 'DAY_REVIEW') return TONES[e.outcome] || 'hold';
    return EVENT_MARKS[e.kind] || 'hold';
  }

  function eventWords(e, t) {  // Figures in the monospace face; the rest as the server wrote.
    const base = pair(t.symbol)[0];
    const box = el('span');
    if (e.kind === 'BUY') {
      add(box, 'Bought ', n(quantity(e.qty)), ' ' + base + ' at ', n(price(e.price)),
        num(e.limit) !== null ? ', limit ' : null, num(e.limit) !== null ? n(price(e.limit)) : null,
        num(e.value_usd) !== null ? ' · ' : null,
        num(e.value_usd) !== null ? n(money(e.value_usd)) : null);
    } else if (e.kind === 'LEVELS_SET') {
      add(box, 'Stop set at ', n(price(e.stop)), ', target ', n(price(e.target)),
        t.tag === 'Fixed exit' ? ' (fixed exit)' : null);
    } else if (e.kind === 'EXIT' && num(e.price) !== null) {
      add(box, 'Sold ', n(quantity(e.qty)), ' ' + base + ' at ', n(price(e.price)),
        ': ' + (e.reason || 'closed'));
    } else if (e.kind === 'RESULT') {
      add(box, e.fees_pending ? 'Fees pending' : add(el('span'), 'Fees ', n(money(e.fees_usd))),
        ' · P&L ', n(money(e.pnl_usd, true), tone(e.pnl_usd)),
        e.fees_pending ? ' before fees' : null,
        num(e.pnl_r) !== null ? ' (' : null, num(e.pnl_r) !== null ? n(rText(e.pnl_r)) : null,
        num(e.pnl_r) !== null ? ')' : null);
    } else {
      box.textContent = e.text;
    }
    return box;
  }

  function timeline(kind, t) {
    const list = el('ol', 'timeline');
    let day = null;
    const row = (cls, when, markKind, ...content) => {
      const li = el('li', cls);
      const time = el('span', 't');
      if (when !== null) {
        const today = dayShort(when);
        if (today !== day) time.append(el('span', 'd', today));
        day = today;
        time.append(text(hhmm(when)));
        time.title = stamp(when);
      } else {
        time.textContent = '…';
      }
      li.append(time, mark(markKind), add(el('span', 'x'), ...content));
      list.append(li);
    };
    for (const e of t.events || []) {
      const since = e.since || e.at;
      const when = since ? Date.parse(since) : null;
      const until = e.repeats > 1 && e.at && e.at !== since
        ? el('span', 'until', 'to ' + hhmm(e.at)) : null;
      row('ev ' + String(e.kind).toLowerCase(), when, eventMark(e), eventWords(e, t), until,
        confidence(e.confidence), e.note ? el('span', 'note', e.note) : null);
    }
    if (kind === 'open' && t.closing) {
      row('ev upcoming', serverNow(), 'flag', 'Exit in progress');
    }
    if (kind === 'open' && t.next_review_at) {
      row('ev upcoming', Date.parse(t.next_review_at), 'hold', 'Next 24-hour review, ',
        rel(t.next_review_at, 'until'));
    }
    if (!list.childElementCount) return empty('No events recorded.');
    return list;
  }

  // --- The price chart -----------------------------------------------------------------------

  function chartWindow(t, closed) {  // From an hour before the entry to an hour after the exit.
    const now = serverNow();
    const start = Date.parse(t.entry_at) - HOUR;
    let end = closed && t.exit_at ? Date.parse(t.exit_at) + HOUR : now;
    end = Math.min(end, now);
    if (!(end > start + 10 * MINUTE)) end = start + 2 * HOUR;
    return {start, end};
  }

  function timeframe(span) {  // At most 1,000 bars for the chart.
    if (span <= 3.4 * DAY) return ['5Min', '5-minute bars'];
    if (span <= 10 * DAY) return ['15Min', '15-minute bars'];
    if (span <= 40 * DAY) return ['1Hour', 'hourly bars'];
    return ['1Day', 'daily bars'];
  }

  function isoMinute(ms) {
    return new Date(Math.floor(ms / MINUTE) * MINUTE).toISOString().replace('.000Z', 'Z');
  }

  function priceHistory(key, t, win) {  // Cached bars; read again when due.
    const closed = key.startsWith('closed:');
    let entry = bars.get(key);
    const due = entry && !entry.loading && Date.now() - entry.at > BARS_REFRESH_MS &&
      (!closed || entry.state !== 'ok');
    if (!entry || due || entry.symbol !== t.symbol) {
      const [frame, words] = timeframe(win.end - win.start);
      entry = {state: entry && entry.state === 'ok' ? 'ok' : 'loading',
        bars: entry ? entry.bars : [], at: Date.now(), frame, words, symbol: t.symbol,
        loading: true};
      bars.set(key, entry);
      readBars(key, t.symbol, win, entry);
    }
    return entry;
  }

  // Public crypto market data, read by the reader's browser: no key, cookie or referrer, and
  // nothing sent but the coins, the time window and the bar size.
  async function marketData(path, params) {
    const query = new URLSearchParams(params);
    const response = await fetch(MARKET_DATA + path + '?' + query.toString(), {
      credentials: 'omit', referrerPolicy: 'no-referrer', cache: 'no-store'});
    if (!response.ok) throw new Error('HTTP ' + response.status);
    return response.json();
  }

  async function readBars(key, symbol, win, slot) {
    try {
      const body = await marketData('/bars', {symbols: symbol, timeframe: slot.frame,
        start: isoMinute(win.start), end: isoMinute(win.end), limit: '1000'});
      const list = body && body.bars && Array.isArray(body.bars[symbol]) ? body.bars[symbol] : [];
      slot.bars = list.map((b) => ({t: Date.parse(b.t), h: num(b.h), l: num(b.l), c: num(b.c)}))
        .filter((b) => Number.isFinite(b.t) && b.h !== null && b.l !== null && b.c !== null);
      slot.state = slot.bars.length ? 'ok' : 'failed';
    } catch (error) {
      if (!slot.bars.length) slot.state = 'failed';
    }
    slot.loading = false;
    slot.at = Date.now();
    const box = document.querySelector('.chart[data-chart="' + key + '"]');
    if (box) paintChart(box);
  }

  function niceStep(range, count) {
    const raw = range / Math.max(1, count);
    const power = Math.pow(10, Math.floor(Math.log10(raw)));
    const unit = raw / power;
    return (unit < 1.5 ? 1 : unit < 3 ? 2 : unit < 7 ? 5 : 10) * power;
  }

  function stepDigits(step) {
    return Math.max(0, Math.min(10, -Math.floor(Math.log10(step) + 1e-9)));
  }

  function tickText(value, digits) {
    return value.toLocaleString(US, {minimumFractionDigits: digits,
      maximumFractionDigits: digits});
  }

  const TIME_STEPS = [5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880, 10080]
    .map((minutes) => minutes * MINUTE);

  function timeTicks(start, end, most) {  // Round local times: 14:00, 14:30 ...
    const step = TIME_STEPS.find((s) => (end - start) / s <= most) || TIME_STEPS.at(-1);
    const offset = new Date(start).getTimezoneOffset() * MINUTE;
    const ticks = [];
    for (let at = Math.ceil((start - offset) / step) * step + offset; at <= end; at += step) {
      ticks.push(at);
    }
    return ticks;
  }

  // A level through time, as [known, not known] paths. A span whose step may hide an unlisted
  // change is not known, unless this level is the same at both ends (levels only rise).
  function stepPath(steps, field, end, X, Y) {
    const known = [];
    const unknown = [];
    steps.forEach((s, i) => {
      const value = s[field];
      if (value === null) return;
      const next = steps[i + 1];
      const to = next ? Math.min(next.t, end) : end;
      if (to <= s.t) return;
      let d = 'M' + X(s.t).toFixed(1) + ' ' + Y(value).toFixed(1) + 'H' + X(to).toFixed(1);
      const moves = next && next[field] !== null && next[field] !== value;
      if (moves && next.t < end) d += 'V' + Y(next[field]).toFixed(1);
      (s.known || !moves ? known : unknown).push(d);
    });
    return [known.join(''), unknown.join('')];
  }

  const REVIEW_MARKS = {APPLIED: 'change', FAILED: 'fail', REFUSED: 'fail', DISCARDED: 'fail',
    FLAGGED: 'flag', EXIT: 'flag'};

  function paintCharts(root) {
    for (const box of root.querySelectorAll('.chart[data-chart]')) paintChart(box);
  }

  function paintChart(box) {
    const key = box.dataset.chart;
    const t = tradeByKey(key);
    const note = document.querySelector('.chart-note[data-note="' + key + '"]');
    if (!t || !t.entry_at) {
      box.replaceChildren();
      return;
    }
    const closed = key.startsWith('closed:');
    const win = chartWindow(t, closed);
    const history = priceHistory(key, t, win);
    const width = Math.max(260, Math.floor(box.clientWidth || 640));
    const small = width < 520;
    const height = small ? 230 : 280;
    const m = {top: 16, right: small ? 64 : 76, bottom: 40, left: small ? 2 : 54};
    const plotBottom = height - m.bottom - 14;
    const laneY = plotBottom + 8;
    const x0 = m.left;
    const x1 = width - m.right;
    const tradeEnd = closed && t.exit_at ? Math.min(Date.parse(t.exit_at), win.end)
      : win.end;
    const series = history.state === 'ok'
      ? history.bars.filter((b) => b.t >= win.start - MINUTE && b.t <= win.end) : [];
    const steps = (t.levels || []).map((s) => ({t: Date.parse(s.at), stop: num(s.stop),
      target: num(s.target), known: s.known !== false})).filter((s) => Number.isFinite(s.t));
    const values = [num(t.entry), num(t.exit), num(t.stop), num(t.target),
      closed ? null : num(t.price)];
    for (const s of steps) values.push(s.stop, s.target);
    for (const b of series) values.push(b.l, b.h);
    const finite = values.filter((v) => v !== null);
    if (!finite.length) {
      box.replaceChildren();
      return;
    }
    let lo = Math.min(...finite);
    let hi = Math.max(...finite);
    const pad = (hi - lo) * 0.08 || Math.abs(hi) * 0.01 || 1;
    lo -= pad;
    hi += pad;
    const X = (ms) => x0 + (Math.min(Math.max(ms, win.start), win.end) - win.start) /
      (win.end - win.start) * (x1 - x0);
    const Y = (v) => m.top + (hi - v) / (hi - lo) * (plotBottom - m.top);
    const chart = svg('svg', {class: 'price', width, height, viewBox: `0 0 ${width} ${height}`,
      role: 'img', 'aria-label': t.symbol + ' price from ' + stamp(win.start) + ' to ' +
        stamp(win.end) + '. Entry ' + price(t.entry) + ', stop ' + price(t.stop) +
        ', target ' + price(t.target) + (closed ? ', exit ' + price(t.exit) : '') + '.'});
    const entryAt = Date.parse(t.entry_at);
    chart.append(svg('rect', {class: 'held', x: X(entryAt), y: m.top,
      width: Math.max(1, X(tradeEnd) - X(entryAt)), height: plotBottom - m.top}));
    const step = niceStep(hi - lo, small ? 4 : 5);
    const digits = stepDigits(step);
    for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
      const y = Math.round(Y(v)) + 0.5;
      chart.append(svg('line', {class: 'gridline', x1: x0, x2: x1, y1: y, y2: y}));
      if (!small) {
        const label = svg('text', {x: x0 - 8, y: y + 4, 'text-anchor': 'end'});
        label.textContent = tickText(v, digits);
        chart.append(label);
      }
    }
    chart.append(svg('line', {class: 'axis', x1: x0, x2: x1, y1: plotBottom + 0.5,
      y2: plotBottom + 0.5}));
    for (const at of timeTicks(win.start, win.end, Math.max(3, (x1 - x0) / 90))) {
      const label = svg('text', {x: X(at), y: height - 8, 'text-anchor': 'middle'});
      const local = new Date(at);
      label.textContent = local.getHours() === 0 && local.getMinutes() === 0 ? dayShort(at)
        : hhmm(at);
      chart.append(label, svg('line', {class: 'axis', x1: X(at), x2: X(at), y1: plotBottom,
        y2: plotBottom + 4}));
    }
    if (series.length) {
      const top = series.map((b, i) => (i ? 'L' : 'M') + X(b.t).toFixed(1) + ' ' +
        Y(b.h).toFixed(1)).join('');
      const bottom = series.slice().reverse().map((b) => 'L' + X(b.t).toFixed(1) + ' ' +
        Y(b.l).toFixed(1)).join('');
      chart.append(svg('path', {class: 'band', d: top + bottom + 'Z'}),
        svg('path', {class: 'line', d: series.map((b, i) => (i ? 'L' : 'M') +
          X(b.t).toFixed(1) + ' ' + Y(b.c).toFixed(1)).join('')}));
    }
    if (num(t.entry) !== null) {
      const y = Y(num(t.entry));
      chart.append(svg('line', {class: 'entry-line', x1: x0, x2: x1, y1: y, y2: y}));
    }
    for (const [field, cls] of [['target', 'target'], ['stop', 'stop']]) {
      const [known, unknown] = stepPath(steps, field, tradeEnd, X, Y);
      if (known) chart.append(svg('path', {class: cls, d: known}));
      if (unknown) chart.append(svg('path', {class: cls + ' unknown', d: unknown}));
    }
    if (num(t.entry) !== null) {
      const x = X(entryAt);
      const y = Y(num(t.entry));
      chart.append(svg('path', {class: 'buy', d: `M${x} ${y - 1}l5 9h-10z`}));
    }
    if (closed && t.exit_at && num(t.exit) !== null) {
      const x = X(Date.parse(t.exit_at));
      const y = Y(num(t.exit));
      chart.append(svg('path', {class: 'sell', d: `M${x} ${y + 1}l5 -9h-10z`}));
    } else if (!closed && num(t.price) !== null) {  // The freshest price the ledger holds.
      const at = t.price_at ? Math.min(Date.parse(t.price_at), win.end) : win.end;
      chart.append(svg('circle', {class: 'lastp', cx: X(at), cy: Y(num(t.price)), r: 3.5}));
    }
    if (t.tag !== 'Fixed exit') {
      chart.append(svg('line', {class: 'lane', x1: x0, x2: x1, y1: laneY + 3.5,
        y2: laneY + 3.5}));
      for (const e of t.events || []) {
        if (e.kind !== 'REVIEW' && e.kind !== 'DAY_REVIEW') continue;
        const a = X(Date.parse(e.since || e.at));
        const b = X(Date.parse(e.at));
        const cls = REVIEW_MARKS[e.outcome] || '';
        const tall = cls === 'change' || cls === 'flag' || e.kind === 'DAY_REVIEW';
        const r = svg('rect', {class: 'rv ' + cls, x: Math.min(a, b) - 1, y: tall ? laneY - 2
          : laneY + 1, width: Math.max(2, Math.abs(b - a) + 2), height: tall ? 11 : 5});
        r.append(add(svg('title'), text(stamp(Date.parse(e.at)) + ' · ' + e.text)));
        chart.append(r);
      }
    }
    const labels = [];
    const level = (value, cls) => {
      if (num(value) !== null) labels.push({y: Y(num(value)), words: price(value), cls});
    };
    level(t.target, 'target-l');
    level(t.stop, 'stop-l');
    level(t.entry, 'entry-l');
    if (closed) level(t.exit, 'exit-l');
    else if (num(t.price) !== null) level(t.price, 'exit-l');
    labels.sort((a, b) => a.y - b.y);
    for (let i = 1; i < labels.length; i++) {
      labels[i].y = Math.max(labels[i].y, labels[i - 1].y + 13);
    }
    const overflow = labels.length ? labels.at(-1).y - (plotBottom + 4) : 0;
    for (const label of labels) {
      const node = svg('text', {class: 'lvl ' + label.cls, x: x1 + 8,
        y: label.y + 4 - Math.max(0, overflow)});
      node.textContent = label.words;
      chart.append(node);
    }
    const cross = svg('line', {class: 'cross', x1: 0, x2: 0, y1: m.top, y2: plotBottom});
    const dot = svg('circle', {class: 'dotp', r: 3, cx: 0, cy: 0});
    const readout = svg('text', {class: 'readout', x: x0 + 6, y: m.top - 4});
    const hit = svg('rect', {class: 'hit', x: x0, y: m.top, width: x1 - x0,
      height: plotBottom - m.top});
    hit.addEventListener('pointermove', (event) => {
      const rect = chart.getBoundingClientRect();
      const px = (event.clientX - rect.left) * (width / Math.max(1, rect.width));
      const at = win.start + (px - x0) / (x1 - x0) * (win.end - win.start);
      let best = null;
      for (const b of series) {
        if (!best || Math.abs(b.t - at) < Math.abs(best.t - at)) best = b;
      }
      const when = best ? best.t : at;
      cross.setAttribute('x1', X(when));
      cross.setAttribute('x2', X(when));
      cross.classList.add('on');
      readout.textContent = stamp(when) + (best ? '  ' + price(best.c) : '');
      if (best) {
        dot.setAttribute('cx', X(when));
        dot.setAttribute('cy', Y(best.c));
      }
      dot.classList.toggle('on', Boolean(best));
    });
    hit.addEventListener('pointerleave', () => {
      cross.classList.remove('on');
      dot.classList.remove('on');
      readout.textContent = '';
    });
    chart.append(cross, dot, readout, hit);
    box.replaceChildren(chart);
    if (note) {
      note.textContent = history.state === 'ok' ? history.words
        : history.state === 'loading' ? 'loading price history…'
          : 'price history unavailable';
    }
  }

  // --- Research ------------------------------------------------------------------------------

  const CYCLE_COLUMNS = [
    {key: 'coin', label: 'Coin'}, {key: 'entry', label: 'Entry', num: true},
    {key: 'stop', label: 'Stop', num: true}, {key: 'target', label: 'Target', num: true},
    {key: 'jev', label: 'Jev'}, {key: 'status', label: 'Status'},
  ];
  const DAY_RUN_COLUMNS = [
    {key: 'slot', label: 'Slot', num: true}, {key: 'run', label: 'Run', num: true},
    {key: 'sent', label: 'Sent', num: true}, {key: 'picks', label: 'Picks', num: true},
    {key: 'selected', label: 'Selected', num: true}, {key: 'traded', label: 'Traded', num: true},
  ];
  const VERDICT_WORDS = {SELECTED: 'Selected', PASSED: 'Passed', VETOED: 'Vetoed',
    NOT_RANKED: 'Not ranked'};

  function coinOf(d) {
    return d.symbol ? pair(d.symbol)[0] : '';
  }

  function actionOf(d) {  // The line's words without its leading coin, starting with a capital.
    const prefix = (d.symbol || '') + ': ';
    const words = d.symbol && d.text.startsWith(prefix) ? d.text.slice(prefix.length) : d.text;
    return words.charAt(0).toUpperCase() + words.slice(1);
  }

  // A coin's latest price: the ledger's while the coin is in a trade (as Positions shows it),
  // else the latest public one-minute bar read for the current cycle.
  function latestPrice(symbol) {
    const held = doc.live_trades.find((t) => t.symbol === symbol && num(t.price) !== null);
    if (held) return {price: num(held.price), at: held.price_at ? Date.parse(held.price_at) : null};
    return latest.prices.get(symbol) || null;
  }

  function entryVersusLast(entry, symbol) {  // "1.24% below" the latest price, or null.
    const last = latestPrice(symbol);
    const level = num(entry);
    if (!last || level === null || !last.price) return null;
    const away = (level - last.price) / last.price * 100;
    const size = Math.abs(away);
    const words = size < 0.005 ? 'at the last price'
      : size.toFixed(size >= 10 ? 1 : 2) + '% ' + (away < 0 ? 'below' : 'above');
    const node = el('span', 'l2', words);
    node.title = 'Last price ' + price(last.price) + (last.at ? ', ' + stamp(last.at) : '');
    return node;
  }

  async function readLatest(symbols) {
    latest.loading = true;
    try {
      const body = await marketData('/latest/bars', {symbols: symbols.join(',')});
      for (const [symbol, bar] of Object.entries((body && body.bars) || {})) {
        const close = num(bar && bar.c);
        if (close !== null) latest.prices.set(symbol, {price: close, at: Date.parse(bar.t)});
      }
      latest.version += 1;
    } catch (error) {
      // The distances stay as they were; the next read is due in a minute.
    }
    latest.loading = false;
    latest.at = Date.now();
    render();
  }

  function wantLatest() {  // Read the cycle's latest prices when due and its table is shown.
    const cycle = doc && doc.agents.cycle;
    if (!cycle || !latest.visible || document.hidden || latest.loading) return;
    const symbols = [...new Set(cycle.picks_list.map((p) => p.symbol).filter(Boolean))];
    const key = symbols.join(',');
    if (!symbols.length || (key === latest.symbols && Date.now() - latest.at < LATEST_REFRESH_MS)) {
      return;
    }
    latest.symbols = key;
    readLatest(symbols);
  }

  const onScreen = 'IntersectionObserver' in window ? new IntersectionObserver((seen) => {
    latest.visible = seen.some((entry) => entry.isIntersecting);
    wantLatest();
  }) : null;

  function watchCycle(node) {
    if (!onScreen) {
      latest.visible = true;
      return;
    }
    onScreen.disconnect();
    if (node) onScreen.observe(node);
    else latest.visible = false;
  }

  function verdictCell(p) {
    const words = VERDICT_WORDS[p.jev] || (p.jev ? p.jev : DASH);
    const rank = num(p.jev_rank) !== null && p.jev !== 'VETOED' ? ' #' + p.jev_rank : '';
    return el('span', 'l1 verdict ' + String(p.jev || 'none').toLowerCase(), words + rank);
  }

  function statusCell(p) {
    const first = el('span', 'l1 status ' + String(p.status).toLowerCase(), p.status_text);
    if (num(p.trade_pnl_usd) === null) return first;
    return lines(first, el('span', 'l2 ' + tone(p.trade_pnl_usd), money(p.trade_pnl_usd, true)));
  }

  function cycleBlock(cycle, pending) {
    const parts = [];
    const item = (label, ...value) => add(el('span'), el('span', 'k', label), ...value);
    if (cycle) {
      const who = cycle.agents.length > 1 ? ' · ' + cycle.agents.join(', ') : '';
      parts.push(el('h3', 'subhead', 'Current cycle · run ' + cycle.run_no + who));
      parts.push(add(el('p', 'facts-line cycle-line'),
        item('Slot', n(shortStamp(cycle.run_at))),
        item('Sent', n(hhmm(cycle.received_at))),
        item('Selected', n(hhmm(cycle.ranked_at))),
        item('Valid until', n(shortStamp(cycle.valid_until)), cycle.valid_until
          ? add(el('span', 'sub'), text(' '), rel(cycle.valid_until, cycle.live ? 'until' : 'ago'))
          : null),
        item('Picks', n(cycle.picks + ' · ' + cycle.selected + ' selected · ' + cycle.traded +
          ' traded'))));
    }
    if (pending) {
      parts.push(add(el('p', 'pending-line'), text('Run ' + pending.run_no + ' (slot '),
        n(shortStamp(pending.run_at)), text(') sent '), n(hhmm(pending.received_at)),
        text(' · ' + pending.picks + ' picks, awaiting Jev')));
    }
    if (!cycle) {
      if (!pending) parts.push(empty('No research cycle yet.'));
      watchCycle(null);
      return parts;
    }
    if (!cycle.picks_list.length) {
      parts.push(empty('No picks listed for this cycle.'));
      watchCycle(null);
      return parts;
    }
    const [grid, tbody] = table(CYCLE_COLUMNS, 'cycle', 'Current cycle picks');
    const many = cycle.agents.length > 1;
    for (const p of cycle.picks_list) {
      const [base, quote] = pair(p.symbol);
      const coin = add(document.createDocumentFragment(),
        add(el('span', 'l1'), el('strong', null, base), el('span', 'quote', quote)),
        many || p.kind ? el('span', 'l2', [many ? p.agent : null, p.kind ? p.kind.toLowerCase()
          : null].filter(Boolean).join(' · ')) : null);
      const row = cells(el('tr', p.jev === 'SELECTED' ? 'picked' : 'dim'), CYCLE_COLUMNS, {
        coin, entry: lines(price(p.entry), entryVersusLast(p.entry, p.symbol)),
        stop: price(p.stop), target: price(p.target), jev: verdictCell(p), status: statusCell(p),
      });
      tbody.append(row);
    }
    parts.push(grid);
    watchCycle(grid);
    return parts;
  }

  function todayRunsTable(runs, cycle) {
    if (!runs.length) return empty('No research run yet today.');
    const [grid, tbody] = table(DAY_RUN_COLUMNS, 'day-runs', "Today's research runs");
    for (const r of runs) {
      const current = cycle && cycle.runs.includes(r.run_no);
      tbody.append(cells(el('tr', current ? 'current' : null), DAY_RUN_COLUMNS, {
        slot: hhmm(r.run_at), run: String(r.run_no),
        sent: r.received_at ? hhmm(r.received_at) : DASH, picks: String(r.picks ?? 0),
        selected: r.ranked_at ? String(r.selected ?? 0) : el('span', 'muted', 'awaiting'),
        traded: String(r.traded ?? 0)}));
    }
    return grid;
  }

  function logTime(iso) {  // "14:05", or "Sep 27 14:05" before today (the date can wrap).
    const box = el('span', 't');
    box.title = stamp(iso);
    if (new Date(serverNow()).toDateString() !== new Date(iso).toDateString()) {
      box.append(el('span', 'd', dayShort(iso)), text(' '));
    }
    box.append(text(hhmm(iso)));
    return box;
  }

  const LOG_MARKS = {BUY: 'buy', SELL: 'sell', RUN: 'hold', EXIT_FLAG: 'flag'};
  const PICK_MARKS = {SELECTED: 'change', RANKED: 'change', VETOED: 'flag'};

  function logMark(d) {  // The square before a line; its words carry the meaning.
    if (d.kind === 'SELECTION') return PICK_MARKS[d.outcome] || 'hold';
    if (d.kind === 'REVIEW_ANSWER' || d.kind === 'FLAG_ANSWER') {
      return d.outcome === 'EXIT' ? 'flag' : 'hold';
    }
    return LOG_MARKS[d.kind] || TONES[d.outcome] || 'hold';
  }

  function logList(items, message, withAgent) {
    if (!items.length) return empty(message);
    const list = el('ol', 'log');
    for (const d of items) {
      const li = el('li', 'kind-' + String(d.kind || 'line').toLowerCase());
      const coin = coinOf(d);
      const folded = d.repeats > 1 && d.since && d.since !== d.at
        ? el('span', 'from', 'from ' + hhmm(d.since)) : null;
      li.append(logTime(d.at));
      if (coin) li.append(el('span', 'c', coin));
      li.append(add(el('span', coin ? 'a' : 'a wide'), mark(logMark(d)),
        withAgent && d.agent ? d.agent + ': ' : null, actionOf(d), folded),
      el('span', 'p', num(d.confidence) !== null ? pct(d.confidence) : ''));
      if (d.note) li.append(el('span', 'note ' + tone(d.pnl_usd), d.note));
      list.append(li);
    }
    return list;
  }

  function renderResearch() {
    const a = doc.agents;
    const s = a.schedule;
    const last = a.latest_run;
    const agents = a.research.map((card) => card.agent);
    byId('research-meta').textContent = agents.length ? agents.join(', ') : '';
    const answers = a.research.flatMap((card) => card.decisions)
      .sort((x, y) => Date.parse(y.at) - Date.parse(x.at));
    put(byId('research-body'),
      facts([
        ['Schedule', s ? s.text + (s.times.length === 1 ? ' ' + zoneCity(s.timezone) : '')
          : DASH],
        ['Last run', last ? n(shortStamp(last.run_at)) : DASH,
          last ? add(el('span', 'sub'), rel(last.run_at)) : null],
        ['Next run', s && s.next_run_at ? n(shortStamp(s.next_run_at)) : DASH,
          s && s.next_run_at ? add(el('span', 'sub'), rel(s.next_run_at, 'until')) : null],
        ['Runs', n(String(a.runs_total))],
        ['Picks today', n(String(doc.today.picks))],
        ['Selected today', n(String(doc.today.selected))],
      ]),
      ...cycleBlock(a.cycle, a.pending_run),
      el('h3', 'subhead', "Today's runs · New York"),
      todayRunsTable(a.today_runs, a.cycle),
      el('h3', 'subhead', 'Review answers and exit flags'),
      logList(answers, 'No 24-hour review answers or exit flags yet.', agents.length > 1));
  }

  // --- Jev -----------------------------------------------------------------------------------

  const HEALTH = {'OK': 'ok', 'Breaker open': 'warn', 'Unavailable': 'bad'};
  const BREAKER = {CLOSED: 'Closed', OPEN: 'Open', HALF_OPEN: 'Half-open'};
  const SCOPES = [['cycle', 'This cycle'], ['today', 'Today']];

  function scopeToggle(active, cycleKnown) {
    const group = el('div', 'scope');
    group.setAttribute('role', 'group');
    group.setAttribute('aria-label', 'Decisions shown');
    for (const [key, words] of SCOPES) {
      const button = el('button', 'scope-btn', words);
      button.type = 'button';
      button.dataset.focus = 'scope:' + key;
      button.setAttribute('aria-pressed', String(active === key));
      button.disabled = key === 'cycle' && !cycleKnown;
      button.addEventListener('click', () => {
        if (jevScope === key) return;
        jevScope = key;
        drawn.delete('jev');
        renderJev();
        const again = document.querySelector('[data-focus="scope:' + key + '"]');
        if (again) again.focus({preventScroll: true});
      });
      group.append(button);
    }
    return group;
  }

  function renderJev() {
    const j = doc.agents.jev;
    const sel = j.latest_selection;
    const dot = el('span', 'dot');
    dot.setAttribute('aria-hidden', 'true');
    const health = add(el('span', 'health ' + (HEALTH[j.health] || '')), dot, text(j.health));
    byId('jev-meta').textContent = 'AI judge';
    const selection = sel ? add(el('p', 'selection'), el('span', 'k', 'Last selection'),
      text('Run ' + sel.run_no + ' · '), n(sel.selected + ' of ' + sel.picks),
      text(' selected · ' + sel.passed + ' passed · ' + sel.vetoed + ' vetoed · ' +
        sel.not_ranked + ' not ranked · '), rel(sel.at)) : null;
    // The log: this cycle (until the first selection, today) or the New York day. The reader's
    // scroll position stays across refreshes until the scope or the cycle changes.
    const active = jevScope === 'cycle' && j.cycle ? 'cycle' : 'today';
    const scope = active === 'cycle' ? j.cycle : j.today;
    const identity = active === 'cycle' ? 'cycle:' + j.cycle.run_no : 'today:' + j.today.day;
    const before = byId('jev-body').querySelector('.log-scroll');
    const keep = before && before.dataset.identity === identity ? before.scrollTop : 0;
    const count = scope.lines.length + (scope.more ? '+' : '') + ' ' +
      (scope.lines.length === 1 ? 'line' : 'lines');
    const meta = el('span', 'log-meta', (active === 'cycle'
      ? 'run ' + j.cycle.run_no + ' · since ' + shortStamp(j.cycle.since)
      : 'New York day') + ' · ' + count);
    const box = el('div', 'log-scroll');
    box.tabIndex = 0;
    box.dataset.focus = 'jev-log';
    box.dataset.identity = identity;
    box.setAttribute('role', 'region');
    box.setAttribute('aria-label', active === 'cycle' ? "Jev's decisions this cycle"
      : "Jev's decisions today");
    add(box, logList(scope.lines, active === 'cycle' ? 'No decisions this cycle yet.'
      : 'No decisions today yet.', false),
    scope.more ? el('p', 'log-more', 'Older decisions not shown') : null);
    put(byId('jev-body'),
      facts([
        ['Health', health],
        ['Breaker', BREAKER[j.breaker] || DASH],
        ['Trade reviews', j.reviews === 'DISABLED' ? 'Switched off'
          : j.reviews === 'ENABLED' ? 'On' : DASH],
        ['Calls today', n(String(j.calls_today ?? 0))],
        ['Failed reviews today', n(String(j.failed_today ?? 0) + (j.failed_today_more ? '+' : ''),
          j.failed_today ? 'warn' : '')],
        ['Last call', j.last_call_at ? rel(j.last_call_at) : DASH,
          j.last_call_ok === false ? el('span', 'sub warn', 'failed') : null],
      ]),
      selection,
      add(el('div', 'log-head'), el('h3', 'subhead', 'Decisions'),
        scopeToggle(active, Boolean(j.cycle)), meta),
      box);
    box.scrollTop = keep;
  }

  // --- Latest picks --------------------------------------------------------------------------

  const PICK_COLUMNS = [
    {key: 'coin', label: 'Coin'}, {key: 'kind', label: 'Kind'},
    {key: 'entry', label: 'Entry', num: true}, {key: 'stop', label: 'Stop', num: true},
    {key: 'target', label: 'Target', num: true}, {key: 'why', label: 'Why'},
    {key: 'jev', label: 'Jev'},
  ];
  const VERDICTS = {SELECTED: 'Selected', PASSED: 'Passed', VETOED: 'Vetoed',
    NOT_RANKED: 'Not ranked'};

  function picksBlock(a) {
    const [grid, tbody] = table(PICK_COLUMNS, 'picks picks-table', 'Latest picks of ' + a.agent);
    for (const p of a.picks_list) {
      const [base, quote] = pair(p.symbol);
      const verdict = add(el('span', 'l1 verdict ' + String(p.jev || '').toLowerCase()),
        text((VERDICTS[p.jev] || DASH) + (num(p.jev_rank) !== null && p.jev !== 'VETOED'
          ? ' · #' + p.jev_rank : '')));
      tbody.append(cells(el('tr'), PICK_COLUMNS, {
        coin: add(el('strong'), text(base), el('span', 'quote', quote)),
        kind: p.kind ? p.kind.toLowerCase() : DASH,
        entry: price(p.entry), stop: price(p.stop), target: price(p.target),
        why: p.why || DASH,
        jev: lines(verdict, p.trade_no ? 'traded · #' + p.trade_no : null),
      }));
    }
    const selected = a.picks_list.filter((p) => p.jev === 'SELECTED').length;
    const key = 'picks:' + a.agent;
    const box = el('details', 'picks');
    box.open = opened.get(key) === true;
    box.addEventListener('toggle', () => opened.set(key, box.open));
    const summary = add(el('summary'), chevron(), text(plural(a.picks_list.length, 'pick',
      'picks') + ' from ' + a.agent), el('span', 'sub', 'run ' + a.last_run_no + ' · ' +
      selected + ' selected by Jev'));
    summary.dataset.focus = key;
    return add(box, summary, grid);
  }

  function renderPicks() {
    const research = doc.agents.research.filter((a) => a.picks_list.length);
    byId('picks-meta').textContent = '';
    byId('picks-body').replaceChildren(...(research.length ? research.map(picksBlock)
      : [empty('No picks yet.')]));
  }

  // --- Performance ---------------------------------------------------------------------------

  const DAY_COLUMNS = [
    {key: 'day', label: 'Day'}, {key: 'trades', label: 'Trades', num: true},
    {key: 'wl', label: 'W–L', num: true}, {key: 'pnl', label: 'P&L', num: true},
    {key: 'r', label: 'R', num: true}, {key: 'fees', label: 'Fees', num: true},
    {key: 'total', label: 'Total', num: true},
  ];

  function renderPast() {
    const days = doc.past.days;
    const body = byId('past-body');
    byId('past-meta').textContent = days.length ? 'New York days' : '';
    if (!days.length) {
      body.replaceChildren(empty('No closed trades yet.'));
      return;
    }
    const [grid, tbody] = table(DAY_COLUMNS, 'days', 'P&L by day');
    for (const d of days.slice().reverse()) {
      tbody.append(cells(el('tr'), DAY_COLUMNS, {
        day: dayLabel(d.day, !phone), trades: String(d.closed), wl: d.wins + '–' + d.losses,
        pnl: el('span', tone(d.pnl_usd), money(d.pnl_usd, true)),
        r: el('span', tone(d.pnl_r), rText(d.pnl_r)),
        fees: d.fees_pending ? el('span', 'warn', d.fees_pending + ' pending')
          : el('span', 'muted', 'known'),
        total: el('span', tone(d.cumulative_pnl_usd), money(d.cumulative_pnl_usd, true)),
      }));
    }
    const icon = (cls, shape) => {
      const node = svg('svg', {class: 'perf', width: 16, height: 10, 'aria-hidden': 'true'});
      node.append(shape === 'bar' ? svg('rect', {class: 'bar pos', x: 4, y: 1, width: 8,
        height: 9}) : svg('line', {class: cls, x1: 0, x2: 16, y1: 5, y2: 5}));
      return node;
    };
    const legendBox = add(el('div', 'legend'), add(el('span'), icon(null, 'bar'),
      text('Daily P&L')), add(el('span'), icon('total'), text('Total')));
    legendBox.setAttribute('aria-hidden', 'true');
    body.replaceChildren(legendBox, el('div', 'perf-chart'), grid);
    paintPerformance();
  }

  function paintPerformance() {
    const box = document.querySelector('#past .perf-chart');
    if (!box) return;
    const days = doc.past.days;
    const width = Math.max(260, Math.floor(box.clientWidth || 640));
    const height = 200;
    const m = {top: 10, right: 8, bottom: 26, left: 70};
    const values = [0];
    for (const d of days) values.push(num(d.pnl_usd) || 0, num(d.cumulative_pnl_usd) || 0);
    let lo = Math.min(...values);
    let hi = Math.max(...values);
    const pad = (hi - lo) * 0.1 || 1;
    if (lo < 0) lo -= pad;
    if (hi > 0 || lo === hi) hi += pad;
    const x0 = m.left;
    const x1 = width - m.right;
    const Y = (v) => m.top + (hi - v) / (hi - lo) * (height - m.bottom - m.top);
    const band = (x1 - x0) / days.length;
    const cx = (i) => x0 + band * (i + 0.5);
    const barWidth = Math.min(44, band * 0.56);
    const chart = svg('svg', {class: 'perf', width, height, viewBox: `0 0 ${width} ${height}`,
      role: 'img', 'aria-label': 'P&L by New York day and the running total, ' +
        money(days.at(-1).cumulative_pnl_usd, true) + ' after ' + dayLabel(days.at(-1).day)});
    const step = niceStep(hi - lo, 4);
    const digits = step >= 1 ? 0 : 2;
    for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
      const y = Math.round(Y(v)) + 0.5;
      const label = svg('text', {x: x0 - 10, y: y + 4, 'text-anchor': 'end'});
      label.textContent = money(Math.abs(v) < step / 1e6 ? 0 : v, false, digits);
      chart.append(svg('line', {class: Math.abs(v) < step / 1e6 ? 'zero' : 'gridline', x1: x0,
        x2: x1, y1: y, y2: y}), label);
    }
    const every = Math.max(1, Math.ceil(days.length / Math.max(1, (x1 - x0) / 70)));
    days.forEach((d, i) => {
      const value = num(d.pnl_usd) || 0;
      const top = Y(Math.max(0, value));
      const bar = svg('rect', {class: 'bar ' + (value < 0 ? 'neg' : 'pos'),
        x: cx(i) - barWidth / 2, y: top, width: barWidth,
        height: Math.max(1, Y(Math.min(0, value)) - top)});
      bar.append(add(svg('title'), text(dayLabel(d.day) + ' · P&L ' + money(d.pnl_usd, true) +
        ' · total ' + money(d.cumulative_pnl_usd, true))));
      chart.append(bar);
      if (i % every === 0) {
        const label = svg('text', {x: cx(i), y: height - 6, 'text-anchor': 'middle'});
        label.textContent = dayLabel(d.day);
        chart.append(label);
      }
    });
    chart.append(svg('path', {class: 'total', d: days.map((d, i) => (i ? 'L' : 'M') +
      cx(i).toFixed(1) + ' ' + Y(num(d.cumulative_pnl_usd) || 0).toFixed(1)).join('')}));
    days.forEach((d, i) => chart.append(svg('circle', {class: 'pt', cx: cx(i),
      cy: Y(num(d.cumulative_pnl_usd) || 0), r: 3})));
    box.replaceChildren(chart);
  }

  // --- Drawing and polling ------------------------------------------------------------------

  const RENDER = {summary: renderSummary, live: renderLive, closed: renderClosed,
    research: renderResearch, jev: renderJev, picks: renderPicks, past: renderPast};
  const INPUTS = {  // What each section is drawn from.
    summary: () => [doc.overall, doc.today, doc.agents.latest_run, doc.agents.schedule,
      doc.status.started_at],
    live: () => [doc.live_trades],
    closed: () => [doc.past.closed_trades, doc.overall.closed, phone],
    research: () => [doc.agents.research, doc.agents.cycle, doc.agents.pending_run,
      doc.agents.today_runs, doc.agents.latest_run, doc.agents.runs_total, doc.agents.schedule,
      doc.today.picks, doc.today.selected, latest.version,
      doc.live_trades.map((t) => [t.symbol, t.price])],
    jev: () => [doc.agents.jev, jevScope],
    picks: () => [doc.agents.research],
    past: () => [doc.past.days, phone],
  };

  function render() {
    renderHeader();
    const focused = document.activeElement && document.activeElement.dataset
      ? document.activeElement.dataset.focus : null;
    for (const [name, inputs] of Object.entries(INPUTS)) {
      const now = JSON.stringify(inputs());
      if (drawn.get(name) === now) continue;  // Same figures: only the clocks move.
      drawn.set(name, now);
      RENDER[name]();
    }
    if (focused && document.activeElement === document.body) {
      const again = [...document.querySelectorAll('[data-focus]')]
        .find((node) => node.dataset.focus === focused);
      if (again) again.focus({preventScroll: true});
    }
    document.body.dataset.ready = '1';
  }

  function tick() {
    if (!doc) return;
    for (const node of document.querySelectorAll('time[data-rel]')) refreshTime(node);
    renderUpdated();
    wantLatest();
  }

  async function poll() {
    if (document.hidden) return;  // A background tab waits; it refreshes when shown again.
    try {
      const response = await fetch('/api/public/experiment', {cache: 'no-store',
        headers: {Accept: 'application/json'}});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      doc = await response.json();
      receivedAt = Date.now();
      failures = 0;
      render();
    } catch (error) {
      failures += 1;
      renderUpdated();
    }
  }

  function resized() {
    const nowPhone = window.matchMedia(PHONE).matches;
    if (nowPhone !== phone) {
      phone = nowPhone;
      render();
    }
    paintCharts(document);
    paintPerformance();
  }

  function start() {
    const initial = byId('initial-data');
    doc = JSON.parse(initial.textContent);
    receivedAt = Date.now();
    render();
    poll();
    setInterval(poll, POLL_MS);
    setInterval(tick, 1000);
    let timer = 0;
    window.addEventListener('resize', () => {
      clearTimeout(timer);
      timer = setTimeout(resized, 150);
    });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) poll();
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
