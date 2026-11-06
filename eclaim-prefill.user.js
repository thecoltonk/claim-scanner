// ==UserScript==
// @name         eClaim Prefill
// @version      3.2
// @description  Fills the 2020source claim form from the local claim-scanner queue. Never submits.
// @match        https://*.eyefinity.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @connect      127.0.0.1
// ==/UserScript==

(function () {
  'use strict';
  if (window.top !== window.self) return;          // not inside the login iframe
  const API = 'http://127.0.0.1:5000';
  const DX_NO_DOT = false;                         // true -> "H52.13" goes in as "H5213"

  // Claim search page (/claims/claim-form/search): auth # + date of service, then search.
  // Best guess until we see the page's fields.
  const isSearchPage = () => !!document.getElementById('searchClaim-authorizationNumber')
    || /\/claims\/claim-form\/search/.test(location.pathname);
  const textOf = el => [el.getAttribute('formcontrolname'), el.getAttribute('placeholder'), el.getAttribute('aria-label'),
    el.closest('mat-form-field')?.querySelector('mat-label')?.innerText, el.closest('label')?.innerText,
    el.parentElement?.parentElement?.innerText].filter(Boolean).join(' ');
  function fillSearch(c) {
    const inputs = qa('input:not([type=hidden]):not([type=checkbox]):not([type=radio])');
    const auth = document.getElementById('searchClaim-authorizationNumber') || inputs.find(i => /auth/i.test(textOf(i)));
    const dos = document.querySelector('input[placeholder="mm/dd/yyyy"]')
      || inputs.find(i => i !== auth && /service|dos|date/i.test(textOf(i)));
    const a = setEl(auth, c.auth), d = setEl(dos, c.dos);
    const btn = document.getElementById('searchClaim-submit')
      || qa('button').find(b => !b.id.startsWith('cp-') && /^(search|continue|next|go|find)/i.test((b.innerText || '').trim()));
    if (a && d && btn) { GM_setValue('auto', { id: c.id, t: Date.now() }); setTimeout(() => btn.click(), 150); }
    say(a && d ? (btn ? 'Auth # + DOS entered, searching…' : 'Auth # + DOS entered. Click search.')
               : `Couldn't find the ${!a ? 'auth #' : 'date'} box. Send me this page's fields.`);
  }

  const qa = (sel, root = document) => [...root.querySelectorAll(sel)];
  const api = (method, path, body) => new Promise((ok, fail) => GM_xmlhttpRequest({
    method, url: API + path, headers: { 'Content-Type': 'application/json' },
    data: body ? JSON.stringify(body) : undefined,
    onload: r => { try { ok(JSON.parse(r.responseText)); } catch (e) { fail(e); } },
    onerror: () => fail(new Error('claim-scanner not running?')),
  }));

  // Leave a box exactly once, like clicking away. (Two blurs made the $ formatter on 24F
  // turn "$95.00" into "".)
  function blurOnce(el) {
    if (document.activeElement === el) el.blur();                 // real blur
    else el.dispatchEvent(new FocusEvent('blur', { bubbles: false }));  // focus never landed -> one fake blur
  }

  const setNative = (el, v) => Object.getOwnPropertyDescriptor(
    el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype, 'value').set.call(el, v);

  // KeyboardEvent ignores keyCode/charCode/which from scripts; currency masks need them, so set them by hand
  function keyEvent(type, o) {
    const e = new KeyboardEvent(type, o);
    const code = type === 'keypress' ? o.charCode : (/\d/.test(o.key) ? 48 + +o.key : o.key === '.' ? 190 : o.keyCode);
    for (const k of ['keyCode', 'which']) try { Object.defineProperty(e, k, { get: () => code }); } catch (x) {}
    try { Object.defineProperty(e, 'charCode', { get: () => (type === 'keypress' ? o.charCode : 0) }); } catch (x) {}
    return e;
  }

  // Type it like a person: some fields (money, masks) only accept real keystrokes
  function typeInto(el, text) {
    el.focus();
    setNative(el, ''); el.dispatchEvent(new Event('input', { bubbles: true }));
    typeKeys(el, text);
    el.dispatchEvent(new Event('change', { bubbles: true }));
    blurOnce(el);
  }

  function typeKeys(el, text) {
    for (const ch of String(text)) {
      const code = ch.charCodeAt(0);
      const opts = { key: ch, code: /\d/.test(ch) ? 'Digit' + ch : ch === '.' ? 'Period' : '', keyCode: code, charCode: code,
                     which: code, bubbles: true, cancelable: true };
      const down = el.dispatchEvent(keyEvent('keydown', opts));
      const press = el.dispatchEvent(keyEvent('keypress', opts));
      if (down && press) {                           // nothing handled the key -> insert it like a keystroke would
        const st = el.selectionStart ?? el.value.length, en = el.selectionEnd ?? el.value.length;
        setNative(el, el.value.slice(0, st) + ch + el.value.slice(en));
        try { el.setSelectionRange(st + 1, st + 1); } catch (e) {}
        el.dispatchEvent(new InputEvent('input', { bubbles: true, data: ch, inputType: 'insertText' }));
      }
      el.dispatchEvent(keyEvent('keyup', opts));
    }
  }

  // Angular listens for input events: set the value and fire them; if it didn't stick, type it instead
  function setEl(el, v) {
    if (!el || v == null || v === '') return false;
    el.focus();
    setNative(el, String(v));
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    blurOnce(el);
    if (el.value.trim() !== String(v)) typeInto(el, v);
    return el.value.trim() !== '';
  }

  // Money boxes (24F charges, 29 amount paid): accept "95.00" / "$95.00", try the formats masks expect
  const num = x => parseFloat(String(x).replace(/[^\d.]/g, ''));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const moneyOk = (el, v) => !!el && Math.abs(num(el.value) - num(v)) < 0.001;

  // Firefox's own "insert text" = the closest thing to real typing a script can do (masks accept it)
  function insertText(el, text) {
    el.focus();
    if (document.activeElement !== el) return false;   // focus didn't land -> don't touch the page selection
    try { el.setSelectionRange(0, el.value.length); } catch (e) {}
    let ok = false;
    try { ok = document.execCommand('insertText', false, String(text)); } catch (e) {}
    if (!ok) return false;
    el.dispatchEvent(new Event('change', { bubbles: true }));
    blurOnce(el);
    return ok;
  }

  async function setMoney(find, v) {
    // Do exactly what a person does: click in, type "95.00", click away. Firefox's own "insert text"
    // makes REAL input events, and a real blur makes the browser send its own change + blur.
    // No fake change/blur events: those made the $ box read "$95.00" and the page wiped it.
    if (v == null || v === '') return false;
    const s = String(v);
    for (let attempt = 0; attempt < 3; attempt++) {
      const el = find();
      if (!el || el.disabled || el.readOnly) return false;
      el.focus();
      if (document.activeElement === el) {
        try { el.setSelectionRange(0, el.value.length); } catch (e) {}
        let typed = false;
        try { typed = document.execCommand('insertText', false, s); } catch (e) {}
        if (!typed) typeKeys(el, s);                 // fallback: keystrokes, still no fake blur
        el.blur();                                    // real click-away
      } else {
        typeInto(el, s);                              // focus didn't land (rare): best effort
      }
      let stuck = true;                               // the page re-renders right after; make sure it stays
      for (let k = 0; k < 2 && stuck; k++) { await sleep(60); stuck = moneyOk(find(), s); }
      if (stuck) return true;
      await sleep(150);
    }
    return false;
  }

  // Tick a checkbox only if it isn't already (clicking toggles)
  function check(el) {
    if (!el) return false;
    if (!el.checked) el.click();
    return el.checked;
  }

  // Radio by its visible label ("EIN")
  function pickRadio(text) {
    const r = qa('input[type="radio"]').find(x =>
      ((x.closest('mat-radio-button, label') || x.parentElement).innerText || '').trim() === text);
    if (!r) return false;
    if (!r.checked) r.click();
    return true;
  }

  // One service line = smallest container holding its CPT box and both date boxes
  function serviceRows() {
    return qa('input[formcontrolname="cptHcpcsCode"]').map(cpt => {
      let n = cpt.parentElement;
      while (n && qa('input[placeholder="mm/dd/yyyy"]', n).length < 2) n = n.parentElement;
      return n;
    }).filter(Boolean);
  }

  // Sex: click the "M"/"F" control next to a date of birth (0 = patient, box 3; 1 = insured, box 11a)
  function setSex(g, which = 0) {
    const dob = qa('input[formcontrolname="dateOfBirth"]')[which];
    if (!dob || !g) return false;
    let box = dob.parentElement;
    while (box && !/\bSex\b/.test(box.innerText || '')) box = box.parentElement;
    if (!box) return false;
    const hit = qa('mat-radio-button, mat-button-toggle, [role="radio"], button, label, mat-checkbox, span, div', box)
      .find(e => (e.innerText || '').trim() === g && e.querySelectorAll('*').length <= 4);
    if (!hit) return false;
    (hit.querySelector('input, button') || hit).click();
    return true;
  }

  // Fill top to bottom like a person, pausing after every field so the page can react
  // (e.g. the CPT lookup). Every box is looked up fresh, in case the page rebuilt it.
  const PAUSE = 20;
  async function fillClaim(c) {
    const done = [], miss = [], money = [];
    const ok = async (name, r) => { (r ? done : miss).push(name); progress(done.length + miss.length); await sleep(PAUSE); };
    const one = name => document.querySelector(`input[formcontrolname="${name}"]`);
    const sig = i => qa('input[formcontrolname="indicator"]')[i];      // 12, 13, 31 share this name, in that order

    // Patient: box 3 sex, box 12 signature
    if (c.gender) await ok('3 sex', setSex(c.gender));
    await ok('12 signature', check(sig(0)));

    // Insured: box 11a DOB (dependents only), box 13 signature
    if (c.dependent) {
      await ok('11a insured DOB', setEl(qa('input[formcontrolname="dateOfBirth"]')[1], c.insured_dob));
      if (c.insured_gender) await ok('11a insured sex', setSex(c.insured_gender, 1));
      else miss.push('11a insured sex');
    }
    await ok('13 signature', check(sig(1)));

    // Box 21: diagnosis codes A-D
    for (let i = 0; i < Math.min(4, c.hcodes.length); i++) {
      const h = c.hcodes[i];
      await ok('Dx ' + 'ABCD'[i], setEl(qa('input[formcontrolname="diagnosisCode"]')[i], DX_NO_DOT ? h.replace('.', '') : h));
    }

    await ok('24j NPI', setEl(document.getElementById('masterNpi') || one('masterNpi'), c.npi));

    // Box 24: service lines, left to right
    const pointer = 'ABCD'.slice(0, Math.max(1, Math.min(4, c.hcodes.length)));
    for (let i = 0; i < c.cpt.length; i++) {
      const l = c.cpt[i];
      const f = name => serviceRows()[i]?.querySelector(`input[formcontrolname="${name}"]`);
      const dates = () => serviceRows()[i] ? qa('input[placeholder="mm/dd/yyyy"]', serviceRows()[i]) : [];
      if (!serviceRows()[i]) { await ok(`line ${i + 1}`, false); continue; }
      await ok(`line ${i + 1} from`, setEl(dates()[0], c.dos));
      await ok(`line ${i + 1} to`, setEl(dates()[1], c.dos));
      if (l.pos) await ok(`line ${i + 1} POS`, setEl(f('placeOfServiceCode'), l.pos));
      await ok(`line ${i + 1} CPT`, setEl(f('cptHcpcsCode'), l.code));
      await sleep(150);                               // give the CPT lookup a moment
      await ok(`line ${i + 1} pointer`, setEl(f('diagnosisPointersUI'), l.pointer || pointer));
      const fee = { name: `line ${i + 1} charge (24F)`, find: () => f('billedAmount'), v: l.fee };
      await setMoney(fee.find, fee.v); money.push(fee); await sleep(PAUSE);
      await ok(`line ${i + 1} units`, setEl(f('unitCount'), l.units || '1'));
    }

    // Bottom: 25 EIN, 27 accept assignment, 31 signature, 29 amount paid
    await ok('25 ' + c.tax_id_type, pickRadio(c.tax_id_type || 'EIN'));
    await ok('27 accept assignment', check(one('doctorAcceptAssignmentIndicator')));
    await ok('31 signature', check(sig(2)));
    const paid = { name: '29 amount paid', find: () => one('patientPaymentAmount'), v: c.copay };
    await setMoney(paid.find, paid.v); money.push(paid);
    return { done, miss, money };
  }

  async function fill(c) {
    if (document.querySelector('input[formcontrolname="cptHcpcsCode"]')) {
      say('Filling…');
      const { done, miss, money } = await fillClaim(c);
      await sleep(300);                               // did the page wipe anything afterwards? refill it
      for (const m of money) if (!moneyOk(m.find(), m.v)) await setMoney(m.find, m.v);
      for (const m of money) {
        const el = m.find();
        if (moneyOk(el, m.v)) done.push(m.name);
        else miss.push(!el ? `${m.name} (box not found)`
          : `${m.name} (box shows "${el.value}"${el.disabled ? ', disabled' : ''}${el.readOnly ? ', read-only' : ''})`);
      }
      GM_setValue('filled', c.id);
      say(miss.length ? `Filled ${done.length}. Couldn't fill: ${miss.join(', ')}.` : `Filled all ${done.length} boxes. Look it over and Submit in eClaim.`, miss.length ? 'bad' : 'good');
      return;
    }
    if (isSearchPage()) return fillSearch(c);
    say('Open Claims → Claim form (search) first.');
  }

  // ---- panel ----
  // Lives in a Shadow DOM: eClaim's styles can't bend it, and it can't touch eClaim's.
  const esc = t => String(t ?? '').replace(/[&<>"]/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[ch]));
  const short = v => /^\d\d\/\d\d\/\d{4}$/.test(v) ? v.slice(0, 5) + '/' + v.slice(8) : v;   // 04/26/1983 -> 04/26/83
  const BLUE = '#006ab6';
  const CSS = `
    :host{all:initial}
    *{box-sizing:border-box}
    .card,.pill{font-family:"Segoe UI",Roboto,"Open Sans",Arial,sans-serif;font-size:13px;line-height:1.4;color:#1f2933}
    .card{width:320px;background:#fff;border:1px solid #c9d3dd;border-radius:6px;box-shadow:0 6px 24px rgba(16,42,67,.18);overflow:hidden}
    .hd{display:flex;align-items:center;gap:8px;background:${BLUE};color:#fff;padding:7px 8px 7px 12px;font-weight:600}
    .hd .q{margin-left:auto;font-weight:400;font-size:12px;opacity:.92}
    .hd button{background:none;border:0;color:#fff;font-size:18px;line-height:1;cursor:pointer;padding:0 4px;font-family:inherit}
    .bd{padding:10px 12px 12px}
    .badge{display:inline-block;font-size:12px;font-weight:600;padding:2px 9px;border-radius:999px;margin-bottom:8px}
    .badge.ok{background:#e6f4ea;color:#1e7b34}.badge.warn{background:#fff4e5;color:#9a5b00}.badge.missing{background:#fdecea;color:#b3261e}
    dl{display:grid;grid-template-columns:74px 1fr;gap:2px 8px;margin:0 0 10px}
    dt{color:#5f6b7a}
    dd{margin:0;font-weight:600;font-variant-numeric:tabular-nums}
    dd.warn{color:#9a5b00}dd.missing{color:#b3261e}
    dd.warn::before{content:"⚠ "}dd.missing::before{content:"✗ "}
    .empty{color:#5f6b7a;padding:6px 0 10px}
    .ck{border:1px solid #f0c36d;background:#fffaf0;border-radius:5px;padding:8px;margin-bottom:8px}
    .ck.missing{border-color:#e8a29c;background:#fff5f4}
    .ck .t{font-weight:600;margin-bottom:6px}
    .ck canvas{display:block;max-width:100%;border:1px solid #e3e8ee;border-radius:3px;margin-bottom:6px;background:#fff}
    .ck canvas.wait{width:100%;height:34px;background:#f3f5f8}
    .chips{display:flex;gap:4px;flex-wrap:wrap}
    .chip{flex:1 0 auto;padding:5px 8px;border:1px solid ${BLUE};background:#fff;color:${BLUE};border-radius:4px;
          font-family:inherit;font-size:12px;font-weight:600;cursor:pointer;font-variant-numeric:tabular-nums}
    .chip:hover{background:#e8f1fa}.chip.on{background:${BLUE};color:#fff}.chip.on:hover{background:#00559a}
    .go{width:100%;height:38px;border:0;border-radius:4px;background:${BLUE};color:#fff;font-family:inherit;font-size:14px;font-weight:600;
        cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px}
    .go:hover{background:#00559a}.go:active{transform:translateY(1px)}
    .go.billed{background:#2e7d32}.go.billed:hover{background:#256b29}
    .go:disabled{background:#d5dce4;color:#5f6b7a;cursor:default;transform:none}
    .go kbd{font-family:inherit;font-size:11px;font-weight:400;background:rgba(255,255,255,.22);border-radius:3px;padding:0 5px}
    .note{font-size:12px;color:#9a5b00;margin:-2px 0 8px}
    .links{display:flex;gap:14px;margin-top:9px;font-size:12px}
    .links button{background:none;border:0;padding:0;color:${BLUE};cursor:pointer;font-family:inherit;font-size:12px}
    .links button:hover{text-decoration:underline}
    .links .del{color:#b3261e;margin-left:auto}
    .links button:disabled{color:#a0aab5;cursor:default;text-decoration:none}
    .msg{margin-top:8px;font-size:12px;color:#5f6b7a;min-height:16px}
    .msg.good{color:#1e7b34}.msg.bad{color:#b3261e}
    .pill{background:${BLUE};color:#fff;border:0;border-radius:999px;padding:8px 14px;font-weight:600;cursor:pointer;
          box-shadow:0 4px 14px rgba(16,42,67,.25)}
    button:focus-visible{outline:2px solid #ffbf47;outline-offset:1px}`;
  const host = document.createElement('div');
  host.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483647';
  const root = host.attachShadow({ mode: 'open' });
  root.innerHTML = `<style>${CSS}</style><div id="ui"></div>`;
  document.body.appendChild(host);
  const ui = root.getElementById('ui');
  const $ = id => root.getElementById(id);

  let claim = GM_getValue('current', null), left = null, busy = false, msg = ['', ''], armDel = 0, formSeen = 0, lastActual = null;
  const pageKind = () => document.querySelector('input[formcontrolname="cptHcpcsCode"]') ? 'form' : isSearchPage() ? 'search' : 'other';
  let page = pageKind();
  const filled = () => !!claim && GM_getValue('filled', null) === claim.id;
  function say(m, kind = '') { msg = [m, kind]; const el = $('cp-msg'); if (el) { el.textContent = m; el.className = 'msg ' + kind; } }
  function progress(n) { say(`Filling… ${n}`); }
  function setClaim(c) {                          // keep the answers you picked with the ⚠ buttons
    const p = GM_getValue('picks', null);
    if (c && p && p.id === c.id) Object.assign(c, p.vals);
    claim = c; GM_setValue('current', c);
  }

  // handwriting crops: fetched once, kept as bitmaps (no flicker when the panel redraws)
  const crops = new Map();
  function paintCrops() {
    if (!claim) return;
    root.querySelectorAll('canvas[data-field]').forEach(cv => {
      const k = claim.id + '/' + cv.dataset.field, v = crops.get(k);
      if (v === 'none') return cv.remove();
      if (v) { cv.className = ''; cv.width = v.width; cv.height = v.height; cv.getContext('2d').drawImage(v, 0, 0); return; }
      cv.className = 'wait';
      if (crops.has(k)) return;                   // already on its way
      crops.set(k, null);
      GM_xmlhttpRequest({
        method: 'GET', url: `${API}/api/crop/${claim.id}/${cv.dataset.field}`, responseType: 'blob',
        onload: async r => {
          let bmp = 'none';
          try { if (r.status === 200) bmp = await createImageBitmap(r.response); } catch (e) {}
          crops.set(k, bmp); paintCrops();
        },
        onerror: () => { crops.set(k, 'none'); paintCrops(); },
      });
    });
  }

  function primary() {
    if (busy) return { label: busy, dis: true };
    if (!claim) return { label: 'No scanned claims waiting', dis: true };
    if (filled()) return { label: 'Billed ✓ · next claim', cls: 'billed', act: billed };
    if (page === 'search') return { label: 'Search this claim', act: runFill };
    if (page === 'form') return { label: 'Fill claim', act: runFill };
    return { label: 'Open Claims → Claim form', dis: true };
  }

  function render() {
    if (GM_getValue('mini', false)) {
      ui.innerHTML = `<button class="pill" id="cp-open">Claims${left != null ? ' · ' + left : ''} ▴</button>`;
      $('cp-open').onclick = () => { GM_setValue('mini', false); render(); };
      return;
    }
    const c = claim, checks = (c && c.check) || [];
    const lvl = (...fs) => { const x = checks.filter(k => fs.includes(k.field)); return x.some(k => k.level === 'missing') ? 'missing' : x.length ? 'warn' : ''; };
    const nMiss = checks.filter(x => x.level === 'missing').length;
    const badge = !c ? '' : c.confirmed && !nMiss ? '<span class="badge ok">✓ Confirmed on phone</span>'
      : checks.length ? `<span class="badge ${nMiss ? 'missing' : 'warn'}">${checks.length} to check below</span>`
      : '<span class="badge ok">✓ All read confidently</span>';
    const row = (label, val, l) => `<dt>${label}</dt><dd class="${l}">${esc(val) || '?'}</dd>`;
    const p = primary();
    const searched = GM_getValue('auto_last', null);
    ui.innerHTML = `<div class="card">
      <div class="hd"><span>Claim prefill</span><span class="q">${left == null ? '' : left ? left + ' in queue' : 'queue empty'}</span>
        <button id="cp-min" title="Minimize">–</button></div>
      <div class="bd">
        ${c ? `${badge}<dl>
          ${row('Auth #', c.auth, lvl('auth'))}
          ${row('Patient', `${c.dependent ? 'Dependent' : 'Self'} · ${c.gender || '?'}`, lvl('dependent', 'gender'))}
          ${c.dependent ? row('Holder', `${c.insured_dob || '?'} · ${c.insured_gender || '?'}`, lvl('insured_dob', 'insured_gender')) : ''}
          ${row('Service', c.dos, lvl('dos'))}
          ${row('Dx', (c.hcodes || []).join(', '), lvl('hcodes'))}
          ${row('Copay', c.copay !== '' && c.copay != null ? '$' + c.copay : '', lvl('copay'))}
        </dl>
        ${checks.map(x => `<div class="ck ${x.level}"><div class="t">${x.level === 'missing' ? '✗' : '⚠'} ${esc(x.text)}</div>
          ${x.crop ? `<canvas data-field="${esc(x.field)}" class="wait"></canvas>` : ''}
          ${(x.choices || []).length > 1 ? `<div class="chips">${x.choices.map(v => {
            const on = (x.field === 'hcodes' ? (c.hcodes || []).join(' + ') : x.field === 'dependent' ? (c.dependent ? 'Dependent' : 'Self') : c[x.field]) === v;
            return `<button class="chip ${on ? 'on' : ''}" data-pick="${esc(x.field)}" data-val="${esc(v)}">${esc(short(v))}</button>`;
          }).join('')}</div>` : ''}</div>`).join('')}
        ${page === 'form' && !filled() && searched && searched !== c.id ? '<div class="note">This form is from a different search. Check the auth #.</div>' : ''}`
        : '<div class="empty">Scan forms with the phone. They show up here on their own.</div>'}
        <button class="go ${p.cls || ''}" id="cp-go" ${p.dis ? 'disabled' : ''}>${esc(p.label)}${p.dis ? '' : ' <kbd>F2</kbd>'}</button>
        <div class="links">
          <button id="cp-view" ${c ? '' : 'disabled'}>View scan</button>
          ${filled() && page === 'form' ? '<button id="cp-refill">Fill again</button>' : ''}
          <button id="cp-skip" ${c && !busy ? '' : 'disabled'}>Skip</button>
          <button id="cp-del" class="del" ${c && !busy ? '' : 'disabled'}>${Date.now() < armDel ? 'Click again to delete' : 'Delete'}</button>
        </div>
        <div class="msg ${msg[1]}" id="cp-msg">${esc(msg[0])}</div>
      </div></div>`;
    $('cp-min').onclick = () => { GM_setValue('mini', true); render(); };
    $('cp-go').onclick = () => p.act && p.act();
    $('cp-view').onclick = () => c && window.open(`${API}/scan/${c.id}`, '_blank');
    if ($('cp-refill')) $('cp-refill').onclick = runFill;
    $('cp-skip').onclick = skip;
    $('cp-del').onclick = del;
    root.querySelectorAll('button[data-pick]').forEach(b => b.onclick = () => choose(b.dataset.pick, b.dataset.val));
    paintCrops();
  }

  // ⚠ choice buttons: use that answer, and put it straight into eClaim if the form is open
  function choose(field, val) {
    const c = claim; if (!c) return;
    if (field === 'hcodes') c.hcodes = val.split(' + ');
    else if (field === 'dependent') c.dependent = val === 'Dependent';
    else c[field] = val;
    const p = GM_getValue('picks', null);
    const vals = p && p.id === c.id ? p.vals : {};
    vals[field] = c[field]; GM_setValue('picks', { id: c.id, vals }); GM_setValue('current', c);
    let put = false;
    if (field === 'auth') {
      if (isSearchPage()) put = setEl(document.getElementById('searchClaim-authorizationNumber'), val);
    } else if (field === 'dos') {
      if (isSearchPage()) put = setEl(document.querySelector('input[placeholder="mm/dd/yyyy"]'), val);
      serviceRows().slice(0, (c.cpt || []).length).forEach(row => {
        qa('input[placeholder="mm/dd/yyyy"]', row).slice(0, 2).forEach(el => { if (setEl(el, val)) put = true; });
      });
    } else if (field === 'insured_dob') {
      put = setEl(qa('input[formcontrolname="dateOfBirth"]')[1], val);
    } else if (field === 'hcodes') {
      const boxes = qa('input[formcontrolname="diagnosisCode"]');
      c.hcodes.forEach((h, i) => { if (setEl(boxes[i], DX_NO_DOT ? h.replace('.', '') : h)) put = true; });
      for (let i = c.hcodes.length; i < Math.min(4, boxes.length); i++) {      // clear leftover codes
        const el = boxes[i]; if (!el || !el.value) continue;
        el.focus(); setNative(el, '');
        el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); el.blur();
      }
    }
    say(`Using ${short(val)}` + (put ? ' (updated in the form).' : field === 'dependent' ? '. Fill (again) to use it.' : '.'), 'good');
    render();
  }

  async function runFill() {
    if (!claim || busy) return;
    if (page === 'search') GM_setValue('auto_last', claim.id);
    busy = page === 'search' ? 'Searching…' : 'Filling…'; render();
    try { await fill(claim); } finally { busy = false; render(); }
  }

  const v = el => (el && el.value || '').trim();
  const readActual = () => {                      // what actually went into eClaim, to score the handwriting guesses
    const row1 = serviceRows()[0];
    return {
      dos: row1 ? v(qa('input[placeholder="mm/dd/yyyy"]', row1)[0]) : '',
      insured_dob: claim && claim.dependent ? v(qa('input[formcontrolname="dateOfBirth"]')[1]) : '',
      hcodes: v(qa('input[formcontrolname="diagnosisCode"]')[0]),
    };
  };
  const clear = () => { GM_setValue('current', null); GM_setValue('filled', null); GM_setValue('picks', null); GM_setValue('auto', null); claim = null; lastActual = null; };

  async function billed() {                       // you submitted it in eClaim -> drop the scan, load the next one
    const c = claim; if (!c || busy) return;
    busy = 'Saving…'; render();
    try {
      await api('POST', `/api/done/${c.id}`, page === 'form' ? readActual() : (lastActual || {}));
      clear(); await next();
      say(`Auth ${c.auth} billed.${claim ? ' Next one is loaded.' : ''}`, 'good');
    } catch (e) { say(e.message, 'bad'); }
    finally { busy = false; render(); }
  }
  async function skip() {                         // come back to it later
    const c = claim; if (!c || busy) return;
    busy = 'Skipping…'; render();
    try { await api('POST', `/api/skip/${c.id}`); clear(); await next(); say('Skipped. It goes to the back of the queue.'); }
    catch (e) { say(e.message, 'bad'); }
    finally { busy = false; render(); }
  }
  async function del() {                          // junk / duplicate scan: throw it away without billing
    const c = claim; if (!c || busy) return;
    if (Date.now() > armDel) { armDel = Date.now() + 3000; render(); setTimeout(render, 3100); return; }
    armDel = 0; busy = 'Deleting…'; render();
    try { await api('POST', `/api/delete/${c.id}`); clear(); await next(); say('Scan deleted.'); }
    catch (e) { say(e.message, 'bad'); }
    finally { busy = false; render(); }
  }

  let lastSig = '';
  async function next(quiet) {
    try {
      const j = await api('GET', '/api/next');
      if (!claim || (j.claim && j.claim.id === claim.id && !busy)) setClaim(j.claim);  // pick up fixes made on the phone
      left = j.left;
    } catch (e) { if (!quiet) say(e.message, 'bad'); }
    const sig = JSON.stringify([claim, left]);
    if (sig !== lastSig) { lastSig = sig; if (!busy) render(); }
  }

  // F2 = the blue button (Search → Fill → Billed)
  document.addEventListener('keydown', e => {
    if (e.key !== 'F2' || e.repeat) return;
    e.preventDefault();
    const p = primary(); if (p.act) p.act();
  }, true);

  // watch the page: redraw when it changes, and fill the claim form by itself right after a search
  setInterval(() => {
    const now = pageKind();
    if (now !== page) { page = now; formSeen = page === 'form' ? Date.now() : 0; render(); }
    if (page === 'form' && filled()) lastActual = readActual();
    const a = GM_getValue('auto', null);
    if (page === 'form' && claim && !busy && !filled() && a && a.id === claim.id && Date.now() - a.t < 300000
        && Date.now() - formSeen > 600) {        // let the form finish loading first
      GM_setValue('auto', null);
      runFill();
    }
  }, 250);
  setInterval(() => next(true), 4000);            // new scans and phone fixes show up on their own

  render();
  next();
})();
