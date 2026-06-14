// ──────────────────────────────────────────────────────────────────────────
// GEO Audit — Chrome Extension popup
// Single-page mode: analyze the active tab via the backend /analyze endpoint
// (Render IP is on Akamai whitelist, so LG sites work without bot blocks)
// ──────────────────────────────────────────────────────────────────────────

const API_BASE = 'https://my-geo-89ft.onrender.com';

// ── i18n ─────────────────────────────────────────────────────────────────
const STR = {
  ko: {
    brand_sub:       'AI Readability · SSR / 스키마',
    open_web:        '웹앱',
    intro:           '현재 활성 탭을 분석합니다.',
    analyze_btn:     '이 페이지 분석',
    duration_hint:   '분석까지 수 초 소요됩니다.',
    loading_msg:     '점수·스키마 측정 및 baseline 조회 중…',
    csr_label:       'SSR / CSR (baseline)',
    ssr_chars:       'SSR 글자수',
    csr_chars:       'CSR 글자수',
    ratio:           '비율',
    csr_baseline_note: '※ 같은 페이지 타입 표본 평균값 (실측 아님)',
    csr_site_avg_note: '※ 데이터가 충분치 않아 사이트 전체 평균으로 SSR/CSR을 확인했습니다 (실측 아님)',
    csr_unavailable:   'SSR/CSR baseline 데이터가 없습니다.',
    page_type:       '페이지 타입',
    pass_count:      '통과',
    na_count:        '대상 아님',
    items_open:      '항목 펼치기',
    items_close:     '항목 접기',
    err_no_tab:      '활성 탭을 찾을 수 없습니다',
    err_bad_url:     '분석할 수 없는 URL입니다 (chrome:// 등 제외)',
    err_generic:     '오류',
    grade_good:      '훌륭함',
    grade_need:      '개선 필요',
    grade_poor:      '미흡',
    tier_excellent:  '우수',
    tier_good:       '양호',
    tier_partial:    '부분',
    tier_poor:       '미흡',
    tier_blocked:    '차단',
    tier_skipped:    '생략',
    tier_unavailable:'측정불가',
    tier_na:         'N/A',
    cat_performance:    'Performance',
    cat_accessibility:  'Accessibility',
    cat_seo:            'SEO',
    cat_ai_readiness:   'AI Readiness',
  },
  en: {
    brand_sub:       'AI Readability · SSR / Schema',
    open_web:        'Open app',
    intro:           'Analyze the page currently open in your active tab.',
    analyze_btn:     'Analyze this page',
    duration_hint:   'Analysis takes a few seconds.',
    loading_msg:     'Measuring score & schema, loading baseline…',
    csr_label:       'SSR / CSR (baseline)',
    ssr_chars:       'SSR chars',
    csr_chars:       'CSR chars',
    ratio:           'Ratio',
    csr_baseline_note: '※ Sample average for this page type (not measured live)',
    csr_site_avg_note: '※ Not enough data — SSR/CSR checked using the whole-site average (not live)',
    csr_unavailable:   'No SSR/CSR baseline data available.',
    page_type:       'Page type',
    pass_count:      'passed',
    na_count:        'N/A',
    items_open:      'Expand items',
    items_close:     'Collapse items',
    err_no_tab:      'No active tab found',
    err_bad_url:     'URL cannot be analyzed (chrome://, etc.)',
    err_generic:     'Error',
    grade_good:      'Good',
    grade_need:      'Needs Improvement',
    grade_poor:      'Poor',
    tier_excellent:  'Excellent',
    tier_good:       'Good',
    tier_partial:    'Partial',
    tier_poor:       'Poor',
    tier_blocked:    'Blocked',
    tier_skipped:    'Skipped',
    tier_unavailable:'Unavailable',
    tier_na:         'N/A',
    cat_performance:    'Performance',
    cat_accessibility:  'Accessibility',
    cat_seo:            'SEO',
    cat_ai_readiness:   'AI Readiness',
  },
};

let lang = (localStorage.getItem('geoAudit.lang') === 'en') ? 'en' : 'ko';
const t = (key) => (STR[lang] && STR[lang][key]) || STR.ko[key] || key;

// ── util ─────────────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
const $ = (id) => document.getElementById(id);

function gradeKey(g) {
  if (g === 'Good') return 'good';
  if (g === 'Need Improvement') return 'need';
  return 'poor';
}
function gradeLabel(g) { return t('grade_' + gradeKey(g)); }
function tierLabel(tier) { return t('tier_' + tier) || tier; }
function tierFromRatio(r) {
  if (typeof r !== 'number') return 'na';
  if (r >= 0.8) return 'excellent';
  if (r >= 0.6) return 'good';
  if (r >= 0.4) return 'partial';
  return 'poor';
}

const CAT_META = [
  { key: 'performance',   icon: '⚡' },
  { key: 'accessibility', icon: '♿' },
  { key: 'seo',           icon: '🔍' },
  { key: 'ai_readiness',  icon: '🤖' },
];

// ── i18n apply ───────────────────────────────────────────────────────────
function applyI18n() {
  document.documentElement.lang = lang;
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.getAttribute('data-i18n');
    const txt = t(key);
    if (txt) el.textContent = txt;
  });
  // language toggle pill 활성 상태
  document.querySelectorAll('.lang-pill').forEach(p => {
    p.classList.toggle('active', p.getAttribute('data-lang') === lang);
  });
  // 결과가 이미 있으면 재렌더 (라벨이 i18n 의존)
  if (lastResult) renderResult(lastResult);
}

let lastResult = null;

document.addEventListener('DOMContentLoaded', () => {
  // 웹앱 링크
  $('openWebLink').href = API_BASE + '/';

  // manifest version 자동 표시 (자동 버전업 훅이 갱신)
  try {
    const v = chrome.runtime.getManifest().version;
    $('brandVersion').textContent = 'v' + v;
  } catch (_) {}

  applyI18n();

  // 언어 토글 — 양쪽 pill 클릭 모두 처리
  $('langToggle').addEventListener('click', (e) => {
    const pill = e.target.closest('.lang-pill');
    if (!pill) {
      // 빈 영역 클릭 → 반대 언어로 토글
      lang = (lang === 'ko') ? 'en' : 'ko';
    } else {
      lang = pill.getAttribute('data-lang');
    }
    localStorage.setItem('geoAudit.lang', lang);
    applyI18n();
  });

  $('analyzeBtn').addEventListener('click', runAnalyze);
});

// ── Analyze ──────────────────────────────────────────────────────────────
async function runAnalyze() {
  // 활성 탭의 URL 추출
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) return showError(t('err_no_tab'));
  const url = tab.url || '';
  if (!/^https?:\/\//i.test(url)) return showError(t('err_bad_url') + ': ' + url);

  showLoading();

  try {
    const res = await fetch(`${API_BASE}/analyze`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ url, scope: 'all', lightweight: true }),
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try { const d = await res.json(); if (d.detail) detail = d.detail; } catch (_) {}
      throw new Error(detail);
    }
    const data = await res.json();
    data._url = url;
    lastResult = data;
    renderResult(data);
  } catch (err) {
    showError(`${t('err_generic')}: ${err.message || String(err)}`);
  } finally {
    $('analyzeBtn').disabled = false;
  }
}

// ── States ───────────────────────────────────────────────────────────────
function showLoading() {
  $('analyzeBtn').disabled = true;
  $('error').classList.add('hidden');
  $('result').classList.add('hidden');
  $('loading').classList.remove('hidden');
}
function showError(msg) {
  $('loading').classList.add('hidden');
  $('result').classList.add('hidden');
  const box = $('error');
  box.textContent = msg;
  box.classList.remove('hidden');
}

// ── Render ───────────────────────────────────────────────────────────────
function renderResult(data) {
  $('loading').classList.add('hidden');
  $('error').classList.add('hidden');
  $('result').classList.remove('hidden');

  const score = data.score || {};
  const total = score.total ?? '-';
  const grade = score.grade || '-';
  const breakdown = score.breakdown || {};

  // Score header
  $('resUrl').textContent = data._url || data.url || '';
  $('resTotal').textContent = total;

  const g = $('resGrade');
  g.textContent = gradeLabel(grade);
  g.className = 'grade-badge grade-' + gradeKey(grade);

  const pt = data.page_type || {};
  const ptBadge = $('resPageType');
  if (pt.label && pt.id !== 'unknown') {
    ptBadge.textContent = `${t('page_type')}: ${pt.label}`;
  } else {
    ptBadge.textContent = '';
  }

  const pct = typeof total === 'number' ? Math.min(total, 100) : 0;
  $('resBar').style.width = pct + '%';

  // CSR card (baseline reference)
  renderCsr(data);

  // Category cards
  renderCategoryCards(breakdown);
}

// 확장은 SSR/CSR을 직접 측정하지 않고 기존 baseline 데이터를 표시한다.
// status==='baseline' → baseline.basis 가
//   'page_type' : 같은 페이지 타입 표본 평균
//   'site'      : page_type 표본 부족 → 사이트 전체 평균 fallback
// baseline 자체가 없으면 unavailable 안내 (메일 발송 없음).
function renderCsr(data) {
  const csr = data.csr_ratio || {};
  const tierEl = $('csrTier');

  if (csr.status === 'baseline') {
    $('csrData').classList.remove('hidden');
    $('csrUnavail').classList.add('hidden');

    // baseline entry에 tier가 없으면 ratio로 유도 (#37 통과 기준 0.6)
    const tier = csr.tier || tierFromRatio(csr.ratio);
    tierEl.textContent = tierLabel(tier);
    tierEl.className = 'csr-tier-badge tier-' + tier;

    const fmt = (n) => (typeof n === 'number') ? n.toLocaleString() : '-';
    $('ssrChars').textContent = fmt(csr.ssr_chars);
    $('csrChars').textContent = fmt(csr.csr_chars);
    $('csrRatio').textContent = (typeof csr.ratio === 'number') ? Math.round(csr.ratio * 100) + '%' : '-';

    const basis = (csr.baseline || {}).basis;
    const note = $('csrNote');
    note.textContent = t(basis === 'site' ? 'csr_site_avg_note' : 'csr_baseline_note');
    note.classList.remove('hidden');
    return;
  }

  // baseline 자체가 없는 예외 케이스
  $('csrData').classList.add('hidden');
  $('csrUnavail').classList.remove('hidden');
  tierEl.textContent = tierLabel('na');
  tierEl.className = 'csr-tier-badge tier-na';
}

function renderCategoryCards(breakdown) {
  const container = $('categoryCards');
  container.innerHTML = '';

  for (const meta of CAT_META) {
    const c = breakdown[meta.key];
    if (!c) continue;

    const items = c.items || {};
    const passed = c.passed ?? 0;
    const total  = c.total  ?? 0;
    const naCount = Object.values(items).filter(i => i.na === true || i.pass === null).length;
    const pct = (c.max && typeof c.points === 'number') ? Math.round((c.points / c.max) * 100) : 0;
    const barColor = pct >= 80 ? '#10b981' : pct >= 50 ? '#f59e0b' : '#ef4444';

    const card = document.createElement('div');
    card.className = 'cat-card';
    card.dataset.cat = meta.key;

    card.innerHTML = `
      <div class="cat-head">
        <div class="cat-icon cat-icon-${meta.key}">${meta.icon}</div>
        <div class="cat-info">
          <div class="cat-name">${t('cat_' + meta.key)}</div>
          <div class="cat-meta">
            <span>${c.points ?? 0} / ${c.max ?? 0}</span>
            <span class="pill pass">${passed}/${total} ${t('pass_count')}</span>
            ${naCount > 0 ? `<span class="pill na">+${naCount} ${t('na_count')}</span>` : ''}
          </div>
        </div>
        <svg class="cat-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
      </div>
      <div class="cat-bar"><div class="cat-bar-fill" style="width:${pct}%; background:${barColor};"></div></div>
      <div class="cat-items">${renderItems(items)}</div>
    `;

    card.querySelector('.cat-head').addEventListener('click', () => {
      card.classList.toggle('open');
    });

    container.appendChild(card);
  }
}

function renderItems(items) {
  return Object.entries(items).map(([id, it]) => {
    // 3-state: PASS / FAIL / N/A (applies_to_page_types skip)
    const isNa = it.na === true || it.pass === null;
    let cls, icon;
    if (isNa) {
      cls = 'item item-na'; icon = '⊘';
    } else if (it.pass) {
      cls = 'item item-pass'; icon = '✓';
    } else {
      cls = 'item item-fail'; icon = '✕';
    }
    const value = it.value ? `<div class="item-value">${escHtml(it.value)}</div>` : '';
    const hint  = it.hint  ? `<div class="item-hint">${escHtml(it.hint)}</div>`  : '';
    return `
      <div class="${cls}">
        <span class="item-icon">${icon}</span>
        <div class="item-body">
          <div class="item-label">${escHtml(it.label || id)}</div>
          ${value}
          ${hint}
        </div>
      </div>`;
  }).join('') || '<p style="font-size:11px;color:var(--c-text-mute);text-align:center;padding:8px 0;">—</p>';
}
