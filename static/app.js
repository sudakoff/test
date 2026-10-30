/* Semantic TextNavigator — клиент. Вся логика извлечения — на сервере (app/extractor.py, app/resolver.py).
   Здесь только отображение результата и ответы на вопросы по уже вычисленной таблице. */
(() => {
  const $ = (s) => document.querySelector(s);
  let RESULT = null;          // последний ответ /api/analyze
  let DOCS = {};              // doc_id -> документ

  // ---------- загрузка файлов ----------
  const filesInput = $('#files');
  let pickedFiles = [];
  filesInput.addEventListener('change', () => {
    pickedFiles = Array.from(filesInput.files || []);
    $('#fileLabel').textContent = pickedFiles.length ? `Выбрано файлов: ${pickedFiles.length}` : 'Выбрать TXT-файлы…';
    $('#analyzeBtn').disabled = pickedFiles.length === 0;
  });

  function readAsText(file, encoding) {
    return new Promise((res, rej) => {
      const r = new FileReader();
      r.onload = () => res(r.result);
      r.onerror = rej;
      r.readAsText(file, encoding);
    });
  }
  async function readFile(file) {
    let text = await readAsText(file, 'utf-8');
    if (text.includes('\uFFFD')) text = await readAsText(file, 'windows-1251'); // не UTF-8 — пробуем cp1251
    return { name: file.name, content: text };
  }

  function setStatus(msg, isError) {
    const el = $('#status');
    el.textContent = msg || '';
    el.className = 'status' + (isError ? ' error' : '');
  }

  $('#analyzeBtn').addEventListener('click', async () => {
    try {
      setStatus('Читаю файлы…');
      const files = await Promise.all(pickedFiles.map(readFile));
      setStatus('Анализирую…');
      const resp = await fetch('/api/analyze', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ check_date: $('#checkDate').value, files }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || resp.statusText);
      render(data);
      setStatus(`Обработано файлов: ${files.length}`);
    } catch (e) { setStatus('Ошибка: ' + e.message, true); }
  });

  $('#sampleBtn').addEventListener('click', async () => {
    try {
      setStatus('Загружаю примеры…');
      const resp = await fetch('/api/sample?check_date=' + encodeURIComponent($('#checkDate').value));
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || resp.statusText);
      render(data);
      setStatus(`Загружены примеры из documents/ (${data.documents.length} файлов)`);
    } catch (e) { setStatus('Ошибка: ' + e.message, true); }
  });

  // ---------- утилиты ----------
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const TYPE_SHORT = { contract: 'договор', amendment: 'допсоглашение', letter: 'письмо', unknown: 'не распознан' };

  function evLink(ev, label) {
    if (!ev) return '';
    const key = `${ev.doc_id}|${ev.paragraph_index}|${encodeURIComponent(ev.fragment || '')}`;
    return `<a class="ev" data-ev="${esc(key)}" title="${esc(ev.filename)}, абзац ${ev.paragraph_index + 1}">${esc(label || ('основание: ' + ev.filename + ', абз. ' + (ev.paragraph_index + 1)))}</a>`;
  }
  function docLink(ref) {
    return `<a class="ev" data-doc="${esc(ref.doc_id)}">${esc(ref.filename)}</a>`;
  }
  document.addEventListener('click', (e) => {
    const a = e.target.closest('a.ev');
    if (!a) return;
    e.preventDefault();
    if (a.dataset.ev) {
      const [docId, idx, frag] = a.dataset.ev.split('|');
      showEvidence(docId, +idx, decodeURIComponent(frag));
    } else if (a.dataset.doc) {
      openDoc(a.dataset.doc, null, null);
    }
  });

  // ---------- отрисовка ----------
  function render(data) {
    RESULT = data;
    DOCS = Object.fromEntries(data.documents.map((d) => [d.doc_id, d]));
    $('#empty').hidden = true;
    $('#results').hidden = false;
    $('#checkDateLabel').textContent = data.check_date_display;
    renderTable(data);
    renderDocs(data);
    renderViewerSelect(data);
    $('#answer').hidden = true;
    const sel = $('#qContract');
    sel.innerHTML = data.contracts.map((r) => `<option value="${esc(r.number)}">${esc(r.number)}</option>`).join('');
  }

  function fieldCell(f) {
    let cls = f.status === 'ok' ? (f.changed_by ? 'changed' : 'ok') : f.status === 'no_data' ? 'nodata' : 'check';
    let html = `<div class="val ${cls}">${esc(f.display)}</div>`;
    if (f.evidence) html += `<span class="src">${evLink(f.evidence)}</span>`;
    if (f.changed_by) {
      html += `<span class="badge changed">изменено: ${esc(TYPE_SHORT[f.changed_by.doc_type])} ${esc(f.changed_by.number ? '№ ' + f.changed_by.number : '')} от ${esc(f.changed_by.doc_date_display)}</span>`;
      if (f.original) html += `<span class="src muted">было: ${esc(f.original.display)} ${f.original.evidence ? evLink(f.original.evidence, '(основание)') : ''}</span>`;
    }
    if (f.status === 'check') html += `<span class="badge check">требует проверки</span>`;
    if (f.note) html += `<span class="src muted">${esc(f.note)}</span>`;
    return html;
  }

  function pendingCell(row) {
    const parts = [];
    for (const w of row.warnings) parts.push(`<p class="warn">${esc(w)}</p>`);
    for (const f of Object.values(row.fields)) for (const w of f.needs_check) parts.push(`<p class="warn">${esc(w)}</p>`);
    for (const p of row.pending_changes) {
      const badge = p.verdict === 'check' ? '<span class="badge check">требует проверки</span>' : '<span class="badge pending">не согласовано</span>';
      parts.push(`<p class="pend ${p.verdict}">${badge}<b>${esc(p.label)}</b> → ${esc(p.display)}<br>
        <span class="muted">${esc(TYPE_SHORT[p.doc.doc_type])}${p.doc.number ? ' № ' + esc(p.doc.number) : ''}, ${esc(p.doc.doc_date_display)}: ${esc(p.reason)}</span><br>
        ${evLink(p.evidence)}</p>`);
    }
    if (!parts.length) return '<span class="muted">нет</span>';
    return parts.join('');
  }

  function renderTable(data) {
    const tb = $('#contracts tbody');
    if (!data.contracts.length) {
      tb.innerHTML = '<tr><td colspan="5" class="muted">Среди загруженных файлов не найдено ни одного договора (заголовок «Договор … № …»).</td></tr>';
      return;
    }
    tb.innerHTML = data.contracts.map((r) => `
      <tr>
        <td class="num">${esc(r.number)}<br>${r.contract_doc ? `<span class="src">${docLink(r.contract_doc)}</span>` : '<span class="badge check">договор не загружен</span>'}</td>
        <td><div class="val ${r.supplier.status === 'ok' ? '' : 'nodata'}">${esc(r.supplier.display)}</div>${r.supplier.evidence ? `<span class="src">${evLink(r.supplier.evidence)}</span>` : ''}</td>
        <td>${fieldCell(r.fields.amount)}</td>
        <td>${fieldCell(r.fields.delivery_date)}</td>
        <td>${pendingCell(r)}</td>
      </tr>`).join('');
  }

  function renderDocs(data) {
    $('#documents').innerHTML = data.documents.map((d) => `
      <div class="doc" data-doc="${esc(d.doc_id)}">
        <div class="fn">${esc(d.filename)}</div>
        <div class="meta">
          <span class="badge type">${esc(d.doc_type_label)}${d.number ? ' № ' + esc(d.number) : ''}</span>
          ${d.doc_type !== 'letter' ? `<span class="badge ${d.sign_status === 'signed' ? 'ok' : d.sign_status === 'draft' ? 'pending' : 'check'}">${esc(d.sign_status_label)}</span>` : ''}
          <br>дата документа: ${esc(d.doc_date_display)}
          ${d.doc_type === 'amendment' ? `<br>вступает в силу: ${esc(d.effective_date_display)} <span class="muted">(${esc(d.effective_note)})</span>` : ''}
          ${d.contract_refs.length ? `<br>относится к: ${esc(d.contract_refs.join(', '))}` : ''}
        </div>
        ${d.notes.map((n) => `<div class="note">${esc(n)}</div>`).join('')}
      </div>`).join('');
    document.querySelectorAll('.doc').forEach((el) => el.addEventListener('click', () => openDoc(el.dataset.doc, null, null)));
    const ul = data.unlinked_documents;
    $('#unlinked').innerHTML = ul.length
      ? `<p class="warn">Не связаны ни с одним договором: ${ul.map((d) => docLink(d)).join(', ')} — проверьте вручную.</p>` : '';
  }

  // ---------- просмотр исходного текста ----------
  function renderViewerSelect(data) {
    const sel = $('#viewerSelect');
    sel.innerHTML = '<option value="">— исходный текст —</option>' +
      data.documents.map((d) => `<option value="${esc(d.doc_id)}">${esc(d.filename)}</option>`).join('');
    sel.onchange = () => sel.value && openDoc(sel.value, null, null);
  }

  function openDoc(docId, paragraphIndex, fragment) {
    const d = DOCS[docId];
    if (!d) return;
    $('#viewerSelect').value = docId;
    $('#viewerBody').innerHTML = d.paragraphs.map((t, i) => {
      let body = esc(t);
      if (i === paragraphIndex && fragment) {
        const idx = t.indexOf(fragment);
        if (idx >= 0) body = esc(t.slice(0, idx)) + '<mark>' + esc(fragment) + '</mark>' + esc(t.slice(idx + fragment.length));
      }
      return `<p id="p-${i}" class="${i === paragraphIndex ? 'hl' : ''}"><span class="n">${i + 1}</span>${body}</p>`;
    }).join('');
    if (paragraphIndex != null) {
      const el = document.getElementById('p-' + paragraphIndex);
      if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  }

  function showEvidence(docId, paragraphIndex, fragment) {
    const d = DOCS[docId];
    if (!d) return;
    const box = $('#evidence');
    box.hidden = false;
    box.innerHTML = `<b>Основание:</b> ${esc(d.filename)} <span class="badge type">${esc(d.doc_type_label)}</span>, абзац ${paragraphIndex + 1}
      ${fragment ? `<br><span class="muted">ключевой фрагмент:</span> <b>${esc(fragment)}</b>` : ''}
      <div class="q">${esc(d.paragraphs[paragraphIndex] || '')}</div>`;
    openDoc(docId, paragraphIndex, fragment);
  }

  // ---------- вопросы ----------
  function rowByNumber(n) { return RESULT.contracts.find((r) => r.number === String(n).toUpperCase()); }

  function answerField(number, fname) {
    const row = rowByNumber(number);
    if (!row) return `<h3>Договор ${esc(number)}</h3><p class="val nodata">нет данных — договор с таким номером не загружен.</p>`;
    const f = fname === 'supplier' ? { ...row.supplier, label: 'Поставщик', changed_by: null, needs_check: [] } : row.fields[fname];
    let html = `<h3>${esc(f.label)} по договору ${esc(row.number)} на ${esc(RESULT.check_date_display)}</h3>
      <p class="big ${f.status === 'ok' ? '' : 'nodata'}">${esc(f.display)}</p>`;
    if (f.evidence) html += `<p>Основание: ${evLink(f.evidence)}<br><span class="muted">«${esc(f.evidence.quote)}»</span></p>`;
    if (f.changed_by) html += `<p><span class="badge changed">изменено</span> документом ${docLink(f.changed_by)} — ${esc(f.changed_by.reason)}. Было: ${esc(f.original.display)} ${f.original.evidence ? evLink(f.original.evidence, '(основание)') : ''}</p>`;
    if (f.note) html += `<p class="muted">${esc(f.note)}</p>`;
    for (const w of f.needs_check || []) html += `<p class="warn">${esc(w)}</p>`;
    const pend = row.pending_changes.filter((p) => p.field === fname);
    if (pend.length) {
      html += '<p><b>Несогласованные изменения этого условия:</b></p><ul>' + pend.map((p) =>
        `<li><span class="badge ${p.verdict === 'check' ? 'check' : 'pending'}">${p.verdict === 'check' ? 'требует проверки' : 'не согласовано'}</span>
         ${esc(p.display)} — ${docLink(p.doc)} (${esc(p.reason)}) ${evLink(p.evidence, '· цитата')}</li>`).join('') + '</ul>';
    }
    return html;
  }

  function answerBefore(beforeIso) {
    const before = new Date(beforeIso + 'T00:00:00');
    const disp = before.toLocaleDateString('ru-RU');
    const items = [];
    const other = [];
    for (const row of RESULT.contracts) {
      const f = row.fields.delivery_date;
      if (f.value && new Date(f.value + 'T00:00:00') < before) {
        const pend = row.pending_changes.filter((p) => p.field === 'delivery_date');
        items.push(`<li><b>${esc(row.number)}</b> — ${esc(f.display)} ${evLink(f.evidence, '· основание')}
          ${f.changed_by ? `<span class="badge changed">изменено: ${esc(f.changed_by.filename)}</span>` : ''}
          ${f.needs_check.length ? '<span class="badge check">требует проверки</span>' : ''}
          <br>${pend.length ? 'Несогласованные изменения срока: <ul>' + pend.map((p) =>
            `<li><span class="badge ${p.verdict === 'check' ? 'check' : 'pending'}">${p.verdict === 'check' ? 'требует проверки' : 'не согласовано'}</span> → ${esc(p.display)} — ${docLink(p.doc)} (${esc(p.reason)}) ${evLink(p.evidence, '· цитата')}</li>`).join('') + '</ul>'
            : '<span class="muted">несогласованных изменений срока нет</span>'}</li>`);
      } else if (f.status !== 'ok') {
        other.push(`<li>${esc(row.number)} — срок: <i>${esc(f.display)}</i> (не включён в выборку, проверьте вручную)</li>`);
      } else {
        other.push(`<li>${esc(row.number)} — ${esc(f.display)}${f.changed_by ? ` (было ${esc(f.original.display)}, изменено: ${esc(f.changed_by.filename)})` : ''} — позже порога</li>`);
      }
    }
    return `<h3>Поставка раньше ${esc(disp)} (на ${esc(RESULT.check_date_display)})</h3>
      ${items.length ? '<ul>' + items.join('') + '</ul>' : '<p class="muted">таких договоров нет</p>'}
      ${other.length ? '<p class="muted">Остальные:</p><ul class="muted">' + other.join('') + '</ul>' : ''}`;
  }

  function showAnswer(html) { const a = $('#answer'); a.hidden = false; a.innerHTML = html; a.scrollIntoView({ block: 'nearest' }); }
  document.querySelectorAll('.preset button').forEach((b) => b.addEventListener('click', () => {
    if (!RESULT) return;
    showAnswer(b.dataset.q === 'field' ? answerField(b.dataset.n, b.dataset.f) : answerBefore(b.dataset.d));
  }));
  $('#qAsk').addEventListener('click', () => RESULT && showAnswer(answerField($('#qContract').value, $('#qField').value)));
  $('#qBeforeBtn').addEventListener('click', () => RESULT && showAnswer(answerBefore($('#qBefore').value)));
})();
