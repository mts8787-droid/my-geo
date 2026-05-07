// ── 탭 전환 ──────────────────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById(tab.dataset.tab + 'Panel').classList.add('active');
  });
});

// ── 공통 유틸 ────────────────────────────────────────────────────────────────
function tierInfo(ratio) {
  if (ratio >= 0.8) return { tier: 'excellent', label: '우수', score: 10, color: '#15803d' };
  if (ratio >= 0.5) return { tier: 'good', label: '양호', score: 7, color: '#0d9488' };
  if (ratio >= 0.3) return { tier: 'partial', label: '부분적', score: 4, color: '#b45309' };
  return { tier: 'poor', label: '미흡', score: 0, color: '#b91c1c' };
}

function escHtml(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ── CSR 글자수 측정 (content script 주입) ────────────────────────────────────
async function measureCsrChars(tabId) {
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      const text = document.body.innerText || '';
      return text.replace(/\s+/g, '').length;
    },
  });
  return results[0]?.result || 0;
}

// ── SSR 글자수 측정 (확장 프로그램에서 직접 fetch — 서버 불필요) ─────────────
async function fetchSsrChars(url) {
  try {
    const res = await fetch(url, {
      headers: {
        'User-Agent': 'Mozilla/5.0 (compatible; GEOAudit/1.0)',
        'Accept': 'text/html',
      },
    });
    const html = await res.text();
    const parser = new DOMParser();
    const doc = parser.parseFromString(html, 'text/html');
    doc.querySelectorAll('script, style, noscript, svg, path').forEach(el => el.remove());
    const text = (doc.body?.innerText || '').replace(/\s+/g, '');
    return text.length;
  } catch {
    return null;
  }
}

// ── 단일 페이지 분석 결과 렌더 ───────────────────────────────────────────────
function renderSingleResult(container, url, ssrChars, csrChars) {
  const ratio = csrChars > 0 ? Math.min(ssrChars / csrChars, 1.0) : 0;
  const info = tierInfo(ratio);
  const pct = Math.round(ratio * 100);

  container.innerHTML = `
    <div class="result-card">
      <div class="result-row">
        <span class="label">URL</span>
        <span class="value url-cell" title="${escHtml(url)}">${escHtml(url)}</span>
      </div>
      <div class="result-row">
        <span class="label">SSR 글자수</span>
        <span class="value" style="color:#2563eb">${ssrChars.toLocaleString()}</span>
      </div>
      <div class="result-row">
        <span class="label">CSR 글자수</span>
        <span class="value" style="color:#7c3aed">${csrChars.toLocaleString()}</span>
      </div>
      <div class="result-row">
        <span class="label">SSR/CSR 비율</span>
        <span class="value">${pct}%</span>
      </div>
      <div class="result-row">
        <span class="label">등급</span>
        <span class="tier-badge tier-${info.tier}">${info.label} (${info.score}/10점)</span>
      </div>
      <div class="ratio-bar">
        <div class="ratio-fill" style="width:${pct}%; background:${info.color};"></div>
      </div>
    </div>`;
}

// ── 단일 페이지 분석 ─────────────────────────────────────────────────────────
document.getElementById('analyzeBtn').addEventListener('click', async () => {
  const btn = document.getElementById('analyzeBtn');
  const container = document.getElementById('singleResult');

  btn.disabled = true;
  btn.textContent = '측정 중...';
  container.innerHTML = '<p class="status-msg">SSR/CSR 글자수를 측정하고 있습니다...</p>';

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.url || tab.url.startsWith('chrome://')) {
      container.innerHTML = '<p class="status-msg error">일반 웹페이지에서 사용해주세요.</p>';
      return;
    }

    // CSR 측정 (현재 탭에서 직접)
    const csrChars = await measureCsrChars(tab.id);

    // SSR 측정 (서버 또는 fetch)
    container.innerHTML = '<p class="status-msg">SSR 글자수를 가져오는 중...</p>';
    const ssrChars = await fetchSsrChars(tab.url);

    if (ssrChars === null) {
      container.innerHTML = '<p class="status-msg error">SSR 글자수를 가져올 수 없습니다.</p>';
      return;
    }

    renderSingleResult(container, tab.url, ssrChars, csrChars);
  } catch (err) {
    container.innerHTML = `<p class="status-msg error">오류: ${escHtml(err.message)}</p>`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'SSR/CSR 측정';
  }
});

// ── 스키마 체크 (단일 페이지 스텔스 모드) ────────────────────────────────────
document.getElementById('schemaBtn').addEventListener('click', async () => {
  const btn = document.getElementById('schemaBtn');
  const container = document.getElementById('singleResult');

  btn.disabled = true;
  btn.textContent = '체크 중...';
  container.innerHTML = '<p class="status-msg">스키마 존재유무 및 정합성을 분석하고 있습니다...</p>';

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.url || tab.url.startsWith('chrome://')) {
      container.innerHTML = '<p class="status-msg error">일반 웹페이지에서 사용해주세요.</p>';
      return;
    }

    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => {
        const scripts = document.querySelectorAll('script[type="application/ld+json"]');
        if (scripts.length === 0) return { exists: false, items: [] };
        
        let items = [];
        scripts.forEach((script, idx) => {
          const content = script.innerText.trim();
          let valid = false;
          let type = 'Unknown';
          let error = null;
          
          if (!content) {
            items.push({ index: idx + 1, valid: false, type: 'Empty', error: '내용이 없습니다.' });
            return;
          }
          
          try {
            const parsed = JSON.parse(content);
            valid = true;
            // 리스트 형태일 경우 첫 번째 요소의 type, 혹은 graph 안에 있을 수도 있음
            if (Array.isArray(parsed)) {
               type = parsed.map(p => p['@type']).filter(Boolean).join(', ');
            } else if (parsed['@graph']) {
               type = parsed['@graph'].map(p => p['@type']).filter(Boolean).join(', ');
            } else {
               type = parsed['@type'] || 'Unknown';
            }
          } catch (e) {
            error = e.message;
          }
          
          items.push({ index: idx + 1, valid, type, error });
        });
        
        return { exists: true, items };
      }
    });

    const data = results[0]?.result;
    if (!data) throw new Error("결과를 가져올 수 없습니다.");

    let html = `<div class="result-card">`;
    if (!data.exists) {
      html += `
        <div class="result-row">
          <span class="label">스키마 상태</span>
          <span class="tier-badge tier-poor">미발견 (Fail)</span>
        </div>
        <p style="font-size:11px; color:#64748b; margin-top:8px;">이 페이지에는 JSON-LD 스키마가 존재하지 않습니다.</p>
      `;
    } else {
      const validCount = data.items.filter(i => i.valid).length;
      const totalCount = data.items.length;
      const allValid = validCount === totalCount;
      
      html += `
        <div class="result-row">
          <span class="label">스키마 상태</span>
          <span class="tier-badge ${allValid ? 'tier-excellent' : (validCount > 0 ? 'tier-partial' : 'tier-poor')}">
            ${allValid ? '정합성 통과 (Pass)' : (validCount > 0 ? '일부 오류' : '오류 발생')}
          </span>
        </div>
        <div class="result-row">
          <span class="label">발견된 개수</span>
          <span class="value">${totalCount}개 (정상 ${validCount}개)</span>
        </div>
      `;
      
      data.items.forEach(item => {
        const color = item.valid ? '#166534' : '#ef4444';
        const typeStr = item.type ? escHtml(String(item.type)) : 'Unknown';
        html += `
          <div style="margin-top: 8px; padding: 8px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px;">
            <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
              <span style="font-size:11px; font-weight:700;">#${item.index} 타입: <span style="color:#4f46e5">${typeStr}</span></span>
              <span style="font-size:10px; font-weight:700; color:${color};">${item.valid ? '유효함' : '에러'}</span>
            </div>
            ${item.error ? `<div style="font-size:10px; color:#ef4444; word-break:break-all;">Error: ${escHtml(item.error)}</div>` : ''}
          </div>
        `;
      });
    }
    html += `</div>`;
    container.innerHTML = html;

  } catch (err) {
    container.innerHTML = `<p class="status-msg error">오류: ${escHtml(err.message)}</p>`;
  } finally {
    btn.disabled = false;
    btn.textContent = '스키마 정합성 체크 (Stealth)';
  }
});

// ── 대량 분석 ────────────────────────────────────────────────────────────────
let bulkRunning = false;

document.getElementById('bulkBtn').addEventListener('click', async () => {
  if (bulkRunning) return;

  const urlText = document.getElementById('urlList').value.trim();
  if (!urlText) return;

  const urls = urlText.split('\n')
    .map(u => u.trim())
    .filter(u => u && !u.startsWith('#'))
    .map(u => u.startsWith('http') ? u : 'https://' + u);

  if (urls.length === 0) return;
  if (urls.length > 100) {
    document.getElementById('bulkResult').innerHTML =
      '<p class="status-msg error">최대 100개까지 분석할 수 있습니다.</p>';
    return;
  }

  bulkRunning = true;
  const btn = document.getElementById('bulkBtn');
  const progress = document.getElementById('bulkProgress');
  const statusEl = document.getElementById('bulkStatus');
  const fillEl = document.getElementById('bulkFill');
  const resultEl = document.getElementById('bulkResult');

  btn.disabled = true;
  btn.textContent = '분석 중...';
  progress.style.display = 'block';
  resultEl.innerHTML = '';

  const results = [];

  for (let i = 0; i < urls.length; i++) {
    const url = urls[i];
    const pct = Math.round(((i) / urls.length) * 100);
    statusEl.textContent = `${i + 1}/${urls.length} 분석 중... ${url}`;
    fillEl.style.width = pct + '%';

    try {
      // 백그라운드 탭으로 URL 열기
      const tab = await chrome.tabs.create({ url, active: false });

      // 페이지 로드 완료 대기
      await waitForTabLoad(tab.id);

      // 추가 렌더링 대기 (SPA 등)
      await sleep(2000);

      // CSR 글자수 측정
      const csrChars = await measureCsrChars(tab.id);

      // 탭 닫기
      await chrome.tabs.remove(tab.id);

      // SSR 글자수 (서버에서)
      const ssrChars = await fetchSsrChars(url);

      if (ssrChars !== null) {
        const ratio = csrChars > 0 ? Math.min(ssrChars / csrChars, 1.0) : 0;
        const info = tierInfo(ratio);
        results.push({ url, ssrChars, csrChars, ratio, tier: info.tier, label: info.label, score: info.score });
      } else {
        results.push({ url, ssrChars: 0, csrChars, ratio: null, tier: 'error', label: 'SSR 실패', score: 0 });
      }
    } catch (err) {
      results.push({ url, ssrChars: 0, csrChars: 0, ratio: null, tier: 'error', label: '오류', score: 0, error: err.message });
    }
  }

  fillEl.style.width = '100%';
  statusEl.textContent = `완료! ${results.length}개 URL 분석됨`;

  renderBulkResults(resultEl, results);

  btn.disabled = false;
  btn.textContent = '일괄 측정 시작';
  bulkRunning = false;
});

function waitForTabLoad(tabId) {
  return new Promise((resolve) => {
    const listener = (id, changeInfo) => {
      if (id === tabId && changeInfo.status === 'complete') {
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    };
    chrome.tabs.onUpdated.addListener(listener);
    // 타임아웃 30초
    setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    }, 30000);
  });
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

// ── 대량 결과 렌더 ───────────────────────────────────────────────────────────
function renderBulkResults(container, results) {
  const success = results.filter(r => r.tier !== 'error');
  const avgRatio = success.length > 0
    ? success.reduce((s, r) => s + (r.ratio || 0), 0) / success.length
    : 0;
  const avgInfo = tierInfo(avgRatio);

  let html = `
    <div class="result-card">
      <div class="result-row">
        <span class="label">분석 완료</span>
        <span class="value">${success.length}/${results.length}개 성공</span>
      </div>
      <div class="result-row">
        <span class="label">평균 SSR/CSR 비율</span>
        <span class="value">${Math.round(avgRatio * 100)}%</span>
      </div>
      <div class="result-row">
        <span class="label">평균 등급</span>
        <span class="tier-badge tier-${avgInfo.tier}">${avgInfo.label}</span>
      </div>
    </div>
    <table class="bulk-table">
      <thead>
        <tr>
          <th>#</th>
          <th>URL</th>
          <th>SSR</th>
          <th>CSR</th>
          <th>비율</th>
          <th>등급</th>
        </tr>
      </thead>
      <tbody>`;

  results.forEach((r, i) => {
    const pct = r.ratio !== null ? Math.round(r.ratio * 100) + '%' : '-';
    const tierCls = r.tier !== 'error' ? `tier-${r.tier}` : '';
    html += `
        <tr>
          <td>${i + 1}</td>
          <td class="url-cell" title="${escHtml(r.url)}">${escHtml(r.url)}</td>
          <td>${r.ssrChars ? r.ssrChars.toLocaleString() : '-'}</td>
          <td>${r.csrChars ? r.csrChars.toLocaleString() : '-'}</td>
          <td>${pct}</td>
          <td><span class="tier-badge ${tierCls}">${escHtml(r.label)}</span></td>
        </tr>`;
  });

  html += `</tbody></table>
    <div class="bulk-actions">
      <button class="export-btn" id="exportCsv">CSV 내보내기</button>
      <button class="export-btn" id="exportJson">JSON 내보내기</button>
    </div>`;

  container.innerHTML = html;

  document.getElementById('exportCsv').addEventListener('click', () => exportCsv(results));
  document.getElementById('exportJson').addEventListener('click', () => exportJson(results));
}

// ── 내보내기 ─────────────────────────────────────────────────────────────────
function exportCsv(results) {
  const header = 'URL,SSR글자수,CSR글자수,비율,등급,점수\n';
  const rows = results.map(r => {
    const pct = r.ratio !== null ? Math.round(r.ratio * 100) : '';
    return `"${r.url}",${r.ssrChars},${r.csrChars},${pct}%,${r.label},${r.score}`;
  }).join('\n');
  downloadFile('geo-audit-csr.csv', header + rows, 'text/csv');
}

function exportJson(results) {
  downloadFile('geo-audit-csr.json', JSON.stringify(results, null, 2), 'application/json');
}

function downloadFile(filename, content, type) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// ── 대량 분석 (스키마) ───────────────────────────────────────────────────────
document.getElementById('bulkSchemaBtn').addEventListener('click', async () => {
  if (bulkRunning) return;

  const urlText = document.getElementById('urlList').value.trim();
  if (!urlText) return;

  const urls = urlText.split('\n')
    .map(u => u.trim())
    .filter(u => u && !u.startsWith('#'))
    .map(u => u.startsWith('http') ? u : 'https://' + u);

  if (urls.length === 0) return;
  if (urls.length > 100) {
    document.getElementById('bulkResult').innerHTML =
      '<p class="status-msg error">최대 100개까지 분석할 수 있습니다.</p>';
    return;
  }

  bulkRunning = true;
  const btn = document.getElementById('bulkSchemaBtn');
  const progress = document.getElementById('bulkProgress');
  const statusEl = document.getElementById('bulkStatus');
  const fillEl = document.getElementById('bulkFill');
  const resultEl = document.getElementById('bulkResult');

  btn.disabled = true;
  btn.textContent = '분석 중...';
  progress.style.display = 'block';
  resultEl.innerHTML = '';

  const results = [];

  for (let i = 0; i < urls.length; i++) {
    const url = urls[i];
    const pct = Math.round(((i) / urls.length) * 100);
    statusEl.textContent = `${i + 1}/${urls.length} 스키마 체크 중... ${url}`;
    fillEl.style.width = pct + '%';

    try {
      const tab = await chrome.tabs.create({ url, active: false });
      await waitForTabLoad(tab.id);
      await sleep(1500);

      const scriptRes = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => {
          const scripts = document.querySelectorAll('script[type="application/ld+json"]');
          if (scripts.length === 0) return { exists: false, items: [] };
          let items = [];
          scripts.forEach((script) => {
            const content = script.innerText.trim();
            if (!content) return;
            try {
              const parsed = JSON.parse(content);
              let type = 'Unknown';
              if (Array.isArray(parsed)) {
                 type = parsed.map(p => p['@type']).filter(Boolean).join(', ');
              } else if (parsed['@graph']) {
                 type = parsed['@graph'].map(p => p['@type']).filter(Boolean).join(', ');
              } else {
                 type = parsed['@type'] || 'Unknown';
              }
              items.push({ valid: true, type });
            } catch (e) {
              items.push({ valid: false, error: e.message });
            }
          });
          return { exists: true, items };
        }
      });
      await chrome.tabs.remove(tab.id);

      const data = scriptRes[0]?.result;
      if (!data) throw new Error("결과 없음");
      
      let validCount = 0;
      let allTypes = [];
      if (data.exists) {
        validCount = data.items.filter(it => it.valid).length;
        allTypes = data.items.map(it => it.type).filter(Boolean);
      }
      
      results.push({
         url, 
         exists: data.exists, 
         count: data.items?.length || 0,
         validCount,
         types: [...new Set(allTypes)].join(', ')
      });

    } catch (err) {
      results.push({ url, exists: false, count: 0, validCount: 0, types: '', error: err.message });
    }
  }

  fillEl.style.width = '100%';
  statusEl.textContent = `완료! ${results.length}개 URL 분석됨`;

  renderBulkSchemaResults(resultEl, results);

  btn.disabled = false;
  btn.textContent = '스키마 일괄 체크 (Stealth)';
  bulkRunning = false;
});

function renderBulkSchemaResults(container, results) {
  const success = results.filter(r => !r.error);
  const found = results.filter(r => r.exists);
  
  let html = \`
    <div class="result-card">
      <div class="result-row">
        <span class="label">분석 완료</span>
        <span class="value">\${success.length}/\${results.length}개 성공</span>
      </div>
      <div class="result-row">
        <span class="label">스키마 발견</span>
        <span class="value">\${found.length}개 페이지에서 발견됨</span>
      </div>
    </div>
    <table class="bulk-table">
      <thead>
        <tr>
          <th>#</th>
          <th>URL</th>
          <th>발견 개수</th>
          <th>상태</th>
          <th>타입 / 에러</th>
        </tr>
      </thead>
      <tbody>\`;

  results.forEach((r, i) => {
    let statusHtml = '';
    if (r.error) {
      statusHtml = \`<span class="tier-badge tier-poor">오류</span>\`;
    } else if (!r.exists) {
      statusHtml = \`<span class="tier-badge tier-poor">미발견</span>\`;
    } else if (r.validCount === r.count) {
      statusHtml = \`<span class="tier-badge tier-excellent">Pass</span>\`;
    } else {
      statusHtml = \`<span class="tier-badge tier-partial">일부 에러</span>\`;
    }
    
    let detail = r.error ? escHtml(r.error) : escHtml(r.types);
    if (!detail && r.exists) detail = 'Unknown';
    if (!r.exists && !r.error) detail = '-';
    
    html += \`
        <tr>
          <td>\${i + 1}</td>
          <td class="url-cell" title="\${escHtml(r.url)}">\${escHtml(r.url)}</td>
          <td>\${r.exists ? r.count : '-'}</td>
          <td>\${statusHtml}</td>
          <td style="font-size:10px; word-break:break-all;">\${detail}</td>
        </tr>\`;
  });

  html += \`</tbody></table>
    <div class="bulk-actions">
      <button class="export-btn" id="exportSchemaCsv">CSV 내보내기</button>
    </div>\`;

  container.innerHTML = html;

  document.getElementById('exportSchemaCsv').addEventListener('click', () => {
    const header = 'URL,발견개수,정상개수,타입,오류\\n';
    const rows = results.map(r => {
      return \`"\${r.url}",\${r.count},\${r.validCount},"\${r.types || ''}","\${r.error || ''}"\`;
    }).join('\\n');
    downloadFile('geo-audit-schema-bulk.csv', header + rows, 'text/csv');
  });
}
