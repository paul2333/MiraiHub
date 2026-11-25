/* 数据看板 UI 验证：真实控制台 + 打桩 API，离线跑（不碰上游）。
 *
 * 锁定四件事：
 *   1. 侧栏「概览」组里能看到「数据看板」，点进去 7 张 KPI 卡片齐全；
 *   2. 消耗分布默认柱状图，切「面积图」后 SVG 里出现堆叠路径；
 *   3. 调用分析三视图可切，排行榜有行；
 *   4. 深浅两套主题、桌面/手机两档宽度都不横向溢出。
 *
 * 用法：node tests/verify_stats_dashboard.cjs [outDir]
 */
const fs = require('fs');
const path = require('path');
const http = require('http');
const assert = require('assert');

/* Playwright 在本机有三个可能的位置（Codex 运行时 / 本机项目依赖 / 全局），
   写死其中一条会让脚本在换机器时直接「模块找不到」——按顺序试，能起来就行。 */
function loadPlaywright() {
  const candidates = [
    'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright',
    'E:/project/mirai-mifan-gpt/node_modules/playwright',
    'playwright',
  ];
  const failures = [];
  for (const candidate of candidates) {
    try { return require(candidate); } catch (error) { failures.push(candidate + ': ' + error.code); }
  }
  throw new Error('找不到 playwright，试过：' + failures.join(' | '));
}

const { chromium } = loadPlaywright();

const root = path.resolve(__dirname, '..');
const consoleDir = path.join(root, 'admin', 'console');
const out = process.argv[2] || path.join(root, 'screenshots', 'stats-dashboard');

const buckets = Array.from({ length: 24 }, (_, i) => {
  const hour = String(i).padStart(2, '0');
  return `2026-11-11 ${hour}:00`;
});
const shape = (fn) => buckets.map((_, index) => fn(index));

const dashboard = {
  window: { days: 1, granularity: 'hour', start: '2026-11-11T00:00:00Z', end: '2026-11-12T00:00:00Z', minutes: 1440 },
  filters: { platform: '', model: '' },
  platform_label: '全部平台',
  summary: {
    requests: 868514, successes: 868253, failures: 261, success_rate: 0.9997,
    credits: 2413.76, total_tokens: 1430000000, prompt_tokens: 1080000000,
    completion_tokens: 350000000, cached_tokens: 260000000, cache_hit_rate: 0.2407,
    rpm: 603.14, tpm: 993055.56, avg_latency_ms: 12640, avg_ttfb_ms: 400,
    throughput_tps: 76.8, window_minutes: 1440,
  },
  models: [
    { model: 'gpt-4.1', label: 'GPT-4.1', requests: 312000, successes: 311980, failures: 20, success_rate: 0.9999, tokens: 510000000, credits: 905.2, avg_latency_ms: 12400, avg_ttfb_ms: 380, tps: 82.1 },
    { model: 'claude-sonnet-4', label: 'Claude Sonnet 4', requests: 221000, successes: 220910, failures: 90, success_rate: 0.9996, tokens: 402000000, credits: 712.4, avg_latency_ms: 13800, avg_ttfb_ms: 520, tps: 66.4 },
    { model: 'gemini-2.5-pro', label: 'Gemini 2.5 Pro', requests: 168000, successes: 167980, failures: 20, success_rate: 0.9999, tokens: 268000000, credits: 402.1, avg_latency_ms: 11200, avg_ttfb_ms: 340, tps: 91.2 },
    { model: 'deepseek-v4.1-flash', label: 'DeepSeek V4.1 Flash', requests: 121000, successes: 120930, failures: 70, success_rate: 0.9994, tokens: 190000000, credits: 264.8, avg_latency_ms: 8600, avg_ttfb_ms: 260, tps: 104.6 },
    { model: 'qwen3-235b-a22b', label: 'Qwen3 235B', requests: 31000, successes: 30990, failures: 10, success_rate: 0.9997, tokens: 42000000, credits: 88.4, avg_latency_ms: 10100, avg_ttfb_ms: 300, tps: 77.3 },
    { model: 'gpt-4.1-mini', label: 'GPT-4.1 mini', requests: 15514, successes: 15463, failures: 51, success_rate: 0.9967, tokens: 18000000, credits: 40.9, avg_latency_ms: 9400, avg_ttfb_ms: 280, tps: 88.0 },
  ],
  platforms: [
    { value: 'workbuddy', label: 'WorkBuddy', requests: 520000, success_rate: 0.9998, tokens: 900000000, credits: 1402.4 },
    { value: 'loomy', label: 'Loomy', requests: 210000, success_rate: 0.9996, tokens: 340000000, credits: 610.2 },
    { value: 'trae', label: 'Trae', requests: 138514, success_rate: 0.9991, tokens: 190000000, credits: 401.1 },
  ],
  distribution: {
    buckets,
    series: [
      { name: 'GPT-4.1', metric: 'credits', values: shape((i) => 40 + Math.round(Math.sin(i / 2) * 18) + i) },
      { name: 'Claude Sonnet 4', metric: 'credits', values: shape((i) => 32 + Math.round(Math.cos(i / 3) * 14) + i / 2) },
      { name: 'Gemini 2.5 Pro', metric: 'credits', values: shape((i) => 26 + Math.round(Math.sin(i / 4) * 12)) },
      { name: 'DeepSeek V4.1 Flash', metric: 'credits', values: shape((i) => 18 + Math.round(Math.cos(i / 5) * 9)) },
      { name: 'Qwen3 235B', metric: 'credits', values: shape((i) => 9 + (i % 5)) },
      { name: '其他 3 个模型', metric: 'credits', values: shape((i) => 4 + (i % 3)) },
    ],
  },
  trend: {
    buckets,
    total_requests: shape((i) => 24000 + Math.round(Math.sin(i / 2) * 9000) + i * 320),
    series: [
      { name: 'GPT-4.1', requests: shape((i) => 12000 + Math.round(Math.sin(i / 2) * 4200)), tokens: [], credits: [] },
      { name: 'Claude Sonnet 4', requests: shape((i) => 8000 + Math.round(Math.cos(i / 3) * 3200)), tokens: [], credits: [] },
      { name: 'Gemini 2.5 Pro', requests: shape((i) => 5200 + Math.round(Math.sin(i / 4) * 2100)), tokens: [], credits: [] },
    ],
  },
  ranking: [
    { model: 'gpt-4.1', label: 'GPT-4.1', requests: 312000, share: 0.3592 },
    { model: 'claude-sonnet-4', label: 'Claude Sonnet 4', requests: 221000, share: 0.2544 },
    { model: 'gemini-2.5-pro', label: 'Gemini 2.5 Pro', requests: 168000, share: 0.1934 },
    { model: 'deepseek-v4.1-flash', label: 'DeepSeek V4.1 Flash', requests: 121000, share: 0.1393 },
    { model: 'qwen3-235b-a22b', label: 'Qwen3 235B', requests: 31000, share: 0.0357 },
    { model: 'gpt-4.1-mini', label: 'GPT-4.1 mini', requests: 15514, share: 0.0179 },
  ],
  errors: [
    { kind: 'server', count: 118 },
    { kind: 'soft_rate', count: 74 },
    { kind: 'session_dead', count: 41 },
    { kind: 'transport', count: 28 },
  ],
};

/* 时段状态轴的夹具：25 格（18:00 起、首尾各半格），状态按位置铺开，
   让四条断言都能命中：有绿有黄有红有斜纹有灰。 */
const marketBuckets = Array.from({ length: 25 }, (_, i) => {
  const hour = (18 + i) % 24;
  const label = String(hour).padStart(2, '0') + ':00';
  return {
    label,
    tip: '11-19 ' + label + '–' + String((hour + 1) % 24).padStart(2, '0') + ':00',
    start: new Date(Date.UTC(2026, 10, 19, i, 0)).toISOString(),
    end: new Date(Date.UTC(2026, 10, 19, i + 1, 0)).toISOString(),
    coverage: i === 0 ? 0.3 : i === 24 ? 0.5 : 1,
    partial: i === 0 || i === 24,
  };
});

const AXIS = {
  'gpt-4.1': ['up', 'up', 'up', 'up', 'up', 'up', 'up', 'up', 'up', 'up', 'up', 'up',
              'up', 'up', 'up', 'up', 'up', 'up', 'up', 'up', 'up', 'up', 'up', 'warn', 'up'],
  'claude-sonnet-4': ['idle', 'idle', 'up', 'up', 'up', 'up', 'warn', 'down', 'down', 'up', 'up', 'up',
                      'up', 'up', 'up', 'up', 'up', 'up', 'up', 'up', 'off', 'off', 'up', 'up', 'up'],
  'gemini-2.5-pro': ['down', 'down', 'down', 'off', 'off', 'off', 'off', 'down', 'down', 'down', 'down', 'down',
                     'down', 'down', 'down', 'down', 'down', 'down', 'down', 'idle', 'idle', 'idle', 'idle', 'idle', 'idle'],
  'deepseek-v4.1-flash': Array.from({ length: 25 }, () => 'idle'),
};

function axisCells(itemKey, states) {
  return states.map((state, index) => ({
    state,
    requests: state === 'idle' || state === 'off' ? 0 : 1200,
    success_rate: state === 'up' ? 1 : state === 'warn' ? 0.98 : state === 'down' ? 0.4 : null,
    avg_latency_ms: state === 'idle' || state === 'off' ? 0 : 12000,
    tokens: 0,
    credits: 0,
    cooling: state === 'off' ? 2 : 0,
    // 与服务端一致：首尾两格只覆盖窗口的一部分
    coverage: marketBuckets[index] ? marketBuckets[index].coverage : 1,
    partial: !!(marketBuckets[index] && marketBuckets[index].partial),
  }));
}

function axisSummary(states) {
  const cells = { ok: 0, warn: 0, down: 0, off: 0, idle: 0 };
  states.forEach((state) => { cells[state === 'up' ? 'ok' : state] += 1; });
  const measured = cells.ok + cells.warn + cells.down;
  return {
    cells,
    measured_cells: measured,
    uptime: measured ? Math.round(((cells.ok + cells.warn) / measured) * 10000) / 10000 : null,
  };
}

const market = {
  window_hours: 24,
  bucket_hours: 1,
  buckets: marketBuckets,
  start: '2026-11-11T00:00:00Z',
  end: '2026-11-12T00:00:00Z',
  source: 'rollup',
  total: 4,
  available_models: 3,
  items: [
    { key: 'gpt-4.1', name: 'GPT-4.1', platforms: ['workbuddy'], platform_labels: ['WorkBuddy'],
      models: ['gpt-4.1'], requests: 312000, success_rate: 0.9999, tokens: 510000000,
      credits: 905.2, avg_latency_ms: 12400, avg_ttfb_ms: 380, tps: 82.1,
      pricing: [{ platform: 'workbuddy', platform_label: 'WorkBuddy', model_id: 'gpt-4.1',
                  multiplier: 0.5, free: false }],
      free: false, min_multiplier: 0.5, active_accounts: 5, exhausted_accounts: 0, available: true,
      configured: true, timeline: axisCells('gpt-4.1', AXIS['gpt-4.1']), ...axisSummary(AXIS['gpt-4.1']) },
    { key: 'claude-sonnet-4', name: 'Claude Sonnet 4', platforms: ['workbuddy', 'loomy'],
      platform_labels: ['WorkBuddy', 'Loomy'], models: ['claude-sonnet-4'],
      requests: 221000, success_rate: 0.9996, tokens: 402000000, credits: 712.4,
      avg_latency_ms: 13800, avg_ttfb_ms: 520, tps: 66.4,
      pricing: [{ platform: 'loomy', platform_label: 'Loomy', model_id: 'claude-sonnet-4',
                  multiplier: 0.8, free: false }],
      free: false, min_multiplier: 0.8, active_accounts: 3, exhausted_accounts: 1, available: true,
      configured: true, timeline: axisCells('claude-sonnet-4', AXIS['claude-sonnet-4']),
      ...axisSummary(AXIS['claude-sonnet-4']) },
    { key: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro', platforms: ['trae'],
      platform_labels: ['Trae'], models: ['gemini-2.5-pro'], requests: 168000,
      success_rate: 0.9999, tokens: 268000000, credits: 402.1, avg_latency_ms: 11200,
      avg_ttfb_ms: 340, tps: 91.2,
      pricing: [{ platform: 'trae', platform_label: 'Trae', model_id: 'gemini-2.5-pro',
                  multiplier: 0.4, free: false }],
      free: false, min_multiplier: 0.4, active_accounts: 2, exhausted_accounts: 2, available: false,
      configured: true, timeline: axisCells('gemini-2.5-pro', AXIS['gemini-2.5-pro']),
      ...axisSummary(AXIS['gemini-2.5-pro']) },
    { key: 'deepseek-v4.1-flash', name: 'DeepSeek V4.1 Flash', platforms: ['workbuddy'],
      platform_labels: ['WorkBuddy'], models: ['dfmodel'], requests: 0,
      success_rate: null, tokens: 0, credits: 0, avg_latency_ms: 0, avg_ttfb_ms: 0, tps: 0,
      pricing: [{ platform: 'workbuddy', platform_label: 'WorkBuddy', model_id: 'dfmodel',
                  multiplier: 0, free: true }],
      free: true, min_multiplier: 0, active_accounts: 5, exhausted_accounts: 0, available: true,
      configured: true, timeline: axisCells('deepseek-v4.1-flash', AXIS['deepseek-v4.1-flash']),
      ...axisSummary(AXIS['deepseek-v4.1-flash']) },
  ],
};

const aggregate = {
  items: [
    { key: 'gpt-4.1', name: 'GPT-4.1', platforms: [
      { platform: 'workbuddy', platform_label: 'WorkBuddy', model_id: 'gpt-4.1',
        client_id: 'gpt-4.1-workbuddy', multiplier: 0.5 }] },
    { key: 'deepseek-v4.1-flash', name: 'DeepSeek V4.1 Flash', platforms: [
      { platform: 'workbuddy', platform_label: 'WorkBuddy', model_id: 'dfmodel',
        client_id: 'dfmodel-workbuddy', multiplier: 0 }] },
  ],
};

const server = http.createServer((req, res) => {
  const pathname = new URL(req.url, 'http://localhost').pathname;
  let file;
  if (pathname === '/admin' || pathname === '/admin/') file = path.join(consoleDir, 'index.html');
  else if (pathname.startsWith('/admin/assets/')) file = path.join(consoleDir, pathname.slice('/admin/assets/'.length));
  else if (pathname.startsWith('/static/')) file = path.join(root, 'admin', pathname);
  else file = path.join(root, pathname);
  fs.readFile(file, (err, data) => {
    if (err) { res.writeHead(404); return res.end('not found'); }
    const type = file.endsWith('.js') ? 'text/javascript'
      : file.endsWith('.css') ? 'text/css'
      : file.endsWith('.svg') ? 'image/svg+xml'
      : file.endsWith('.png') ? 'image/png'
      : file.endsWith('.ico') ? 'image/x-icon'
      : 'text/html; charset=utf-8';
    res.writeHead(200, { 'Content-Type': type });
    res.end(data);
  });
});

(async () => {
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  fs.mkdirSync(out, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()); });

    await page.addInitScript(() => {
      localStorage.setItem('wb_admin_token', 'synthetic-token');
      localStorage.setItem('wb_admin_username', 'tester');
    });

    await page.route('**/api/**', async (route) => {
      const url = new URL(route.request().url());
      let data = { items: [], accounts: [], summary: {}, models: [], platforms: [] };
      if (url.pathname === '/api/app/version') data = { version: '1.1.1' };
      else if (url.pathname === '/api/stats/dashboard') data = dashboard;
      else if (url.pathname === '/api/stats/model-market') data = market;
      else if (url.pathname === '/api/models/aggregate') data = aggregate;
      else if (url.pathname === '/api/overview') data = {
        balance: { total: 1000, used: 200 }, today: { requests: 10, tokens: 100 },
        cache: { hit: 0.3 }, accounts: { total: 3, active: 3 },
      };
      else if (url.pathname === '/api/platforms') data = { items: [] };
      else if (url.pathname === '/api/sentinel/status') data = { items: [], signals: [] };
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) });
    });

    await page.goto(`http://127.0.0.1:${server.address().port}/admin`);
    await page.waitForFunction(() => typeof window.activateTab === 'function', null, { timeout: 15000 });
    await page.evaluate(() => activateTab('stats'));

    await page.locator('#panel-stats:not(.hidden)').waitFor();
    await page.locator('.wbd-kpi').first().waitFor();
    // KPI 有 620ms 的滚动动效，断言前先等它落地。
    await page.waitForTimeout(900);

    if (process.env.WBD_DEBUG) {
      console.log('PAGE_ERRORS', JSON.stringify(errors));
      console.log('DIST', await page.evaluate(() => {
        const host = document.getElementById('wbdDistChart');
        return {
          series: window.WBStatsDashboard.state.data.distribution.series.length,
          first: window.WBStatsDashboard.state.data.distribution.series[0].values.slice(0, 3),
          firstType: typeof window.WBStatsDashboard.state.data.distribution.series[0].values[0],
          hostWidth: host ? host.clientWidth : -1,
          buckets: window.WBStatsDashboard.state.data.distribution.buckets.length,
          legend: document.querySelectorAll('.wbd-legend-item').length,
          rects: host ? host.querySelectorAll('rect').length : -1,
          tail: host ? host.innerHTML.slice(-200) : 'MISSING',
        };
      }));
    }

    assert.equal(await page.locator('.wbd-kpi').count(), 7, 'KPI 应有 7 张');
    assert.equal(await page.locator('.wbd-model-chip').count(), 6, '性能健康带应有 6 个模型');
    assert((await page.locator('.wbd-kpi').nth(1).innerText()).includes('2,413.76'), '总积分格式');
    assert((await page.locator('.wbd-kpi').nth(3).innerText()).includes('2.60亿'), '缓存读取应缩写');

    // 柱子：24 个桶 × 6 个系列里凡是 >0 的都应有 rect
    const bars = await page.locator('#wbdDistChart svg rect').count();
    assert(bars >= 24, '消耗分布应有柱子，实际 ' + bars);
    await page.screenshot({ path: path.join(out, 'dashboard-desktop.png'), fullPage: true });

    // 悬停提示：图表柱子必须有气泡（不能只有原生 title 的 1 秒延迟）
    await page.locator('#wbdDistChart svg rect').first().hover();
    await page.locator('#chartTip:not([hidden])').waitFor({ timeout: 3000 });

    // 切面积图
    await page.getByRole('button', { name: '面积图' }).click();
    await page.waitForFunction(() => document.querySelectorAll('#wbdDistChart svg path').length >= 3);
    assert((await page.locator('#wbdDistChart svg path').count()) >= 3, '面积图应有堆叠路径');
    await page.screenshot({ path: path.join(out, 'dashboard-area.png'), fullPage: true });

    // 调用次数分布 / 排行
    await page.getByRole('button', { name: '调用次数分布' }).click();
    await page.waitForFunction(() => document.querySelectorAll('#wbdAnalysisChart svg rect').length > 0);
    await page.getByRole('button', { name: '调用次数排行' }).click();
    await page.locator('.wbd-rank-row').first().waitFor();
    assert.equal(await page.locator('.wbd-rank-row').count(), 6, '排行行数');
    assert((await page.locator('.wbd-rank-row').first().innerText()).includes('35.9%'), '排行占比');
    await page.screenshot({ path: path.join(out, 'dashboard-ranking.png'), fullPage: true });

    // ── 模型广场 ────────────────────────────────────────────────────────
    await page.evaluate(() => activateTab('market'));
    await page.locator('#panel-market:not(.hidden)').waitFor();
    await page.locator('.wbm-card').first().waitFor();
    assert.equal(await page.locator('.wbm-card').count(), 4, '广场卡片数');
    assert((await page.locator('.wbm-card').first().innerText()).includes('×0.50'), '定价显示');
    assert((await page.locator('.wbm-card').nth(3).innerText()).includes('免费'), '免费模型');
    assert((await page.locator('.wbm-card').nth(2).innerText()).includes('当前不可用'), '不可用标记');

    // 左上角必须是真实品牌 logo，不是首字母方块
    assert.equal(await page.locator('.wbm-card .wbm-logo svg').count() >= 3, true,
      '卡片左上角应有品牌图标（至少 gpt / claude / gemini 三个）');
    const logoBrands = await page.evaluate(() =>
      Array.from(document.querySelectorAll('.wbm-card .wbm-logo')).map((node) => node.dataset.brand));
    assert(logoBrands.includes('openai') || logoBrands.includes('deepseek'),
      'logo 应带上品牌标识，实际 ' + JSON.stringify(logoBrands));

    // 底部状态轴：一格一段时间，格数 = 服务端 buckets 长度
    const axis = await page.evaluate(() => {
      const first = document.querySelector('.wbm-card .wbm-cells');
      const cells = Array.from(first.querySelectorAll('.wbm-cell'));
      const all = Array.from(document.querySelectorAll('.wbm-card .wbm-cell'));
      return {
        cellCount: cells.length,
        states: cells.map((cell) => cell.className.replace('wbm-cell ', '').trim()),
        allStates: all.map((cell) => cell.className),
        axisHeads: document.querySelectorAll('.wbm-card .wbm-axis-head').length,
        scales: document.querySelectorAll('.wbm-card .wbm-axis-scale').length,
        legend: document.querySelectorAll('.wbm-legend .wbm-legend-item').length,
        timeout: document.querySelectorAll('.market-meter').length,
      };
    });
    assert.equal(axis.cellCount, 25, '一格 = 一个时间段，格数应等于 buckets 长度');
    assert(axis.states.some((name) => name.includes('is-up')), '应有正常格');
    assert(axis.allStates.some((name) => name.includes('is-idle')), '应有空档格');
    assert(axis.allStates.some((name) => name.includes('is-down')), '应有失败格');
    assert.equal(axis.axisHeads, 4, '每张卡都要写清「近 N 小时 · 每格 X 小时」');
    assert.equal(axis.scales, 4, '每张卡都要有轴标');
    assert.equal(axis.legend, 5, '图例五态齐全');
    // 图例里的色块必须真的有尺寸：它是 i 元素，没有栅格给宽度，忘了写死就是 0×0
    const legendBox = await page.evaluate(() => {
      const node = document.querySelector('.wbm-legend .wbm-cell');
      if (!node) return null;
      const rect = node.getBoundingClientRect();
      return { width: Math.round(rect.width), height: Math.round(rect.height) };
    });
    assert(legendBox && legendBox.width >= 8 && legendBox.height >= 8,
      '图例色块必须有可见尺寸，实际 ' + JSON.stringify(legendBox));
    assert.equal(axis.timeout, 0, '旧的单色成功率条应已移除');

    // 斜纹格（冷却中）必须真的出现在 claude 卡上，并且带 tooltip 文案
    const coolingCells = await page.locator('.wbm-card').nth(1).locator('.wbm-cell.is-off').count();
    assert(coolingCells > 0, 'claude 卡应有「冷却中」斜纹格，实际 ' + coolingCells);
    const tip = await page.locator('.wbm-card').nth(1).locator('.wbm-cell.is-off').first()
      .getAttribute('data-tooltip');
    assert(/冷却中/.test(tip || ''), '斜纹格的 tooltip 要说明是冷却，实际 ' + tip);

    // 柔光必须是「整页一层 + 高斯模糊」，面板自己不再画矩形渐变（那就是白缝与方角的来源）
    const seam = await page.evaluate(() => {
      const panel = document.querySelector('#panel-market');
      const main = document.querySelector('.app-main');
      const glow = getComputedStyle(main, '::before');
      const toolbar = getComputedStyle(panel.querySelector('.panel-toolbar'));
      return {
        layer: glow.backgroundImage,
        filter: glow.filter,
        panelLayer: getComputedStyle(panel, '::before').backgroundImage,
        toolbarBg: toolbar.backgroundImage,
        topbarBg: getComputedStyle(document.querySelector('.desktop-topbar')).backgroundImage,
      };
    });
    assert(seam.layer && seam.layer !== 'none', '顶部柔光应挂在 .app-main::before 上当整页背景层');
    assert(/blur\(/.test(seam.filter), '柔光要有高斯模糊，实际 ' + seam.filter);
    assert.equal(seam.panelLayer, 'none', '面板自己不应再画一层渐变（会露出矩形边界）');
    assert.equal(seam.toolbarBg, 'none', '工具条自己不应再画渐变（会与卡片之间断开）');
    assert(/gradient/.test(seam.topbarBg), '顶栏应是半透明薄纱，而不是一条纯白横带');

    // 五态必须真的是五种颜色（不是同一块灰），且半格要能看出是半格
    const palette = await page.evaluate(() => {
      const pick = (selector) => {
        const node = document.querySelector(selector);
        if (!node) return null;
        const style = getComputedStyle(node);
        return { bg: style.backgroundColor, image: style.backgroundImage, opacity: style.opacity };
      };
      return {
        up: pick('.wbm-cell.is-up'),
        warn: pick('.wbm-cell.is-warn'),
        down: pick('.wbm-cell.is-down'),
        off: pick('.wbm-cell.is-off'),
        idle: pick('.wbm-cell.is-idle'),
        partial: pick('.wbm-cells .wbm-cell.is-partial'),
      };
    });
    assert(palette.up && palette.warn && palette.down && palette.off && palette.idle,
      '五种状态格都应渲染出来，实际 ' + JSON.stringify(palette));
    const colors = [palette.up.bg, palette.warn.bg, palette.down.bg];
    assert.equal(new Set(colors).size, 3, '正常 / 抖动 / 不可用 必须是三种不同颜色：' + colors.join(' '));
    assert(/gradient/.test(palette.off.image), '冷却格应走斜纹，而不是又一个纯色');
    assert(palette.partial && Number(palette.partial.opacity) < 1,
      '只覆盖部分时长的格子要按覆盖率淡出，实际 ' + JSON.stringify(palette.partial));

    // 卡片不能把内容裁掉（标题两行 + 四列数字 + 状态轴 + 页脚，任何一段挤爆都会被这里抓到）
    const clipping = await page.evaluate(() => {
      const problems = [];
      Array.from(document.querySelectorAll('.wbm-card')).forEach((card, index) => {
        if (card.scrollHeight - card.clientHeight > 1) {
          problems.push({ index, kind: 'clipped', text: card.innerText.replace(/\s+/g, ' ').slice(0, 50) });
        }
        const title = card.querySelector('.wbm-title h3');
        const copy = card.querySelector('.wbm-copy');
        if (title && copy) {
          const a = title.getBoundingClientRect();
          const b = copy.getBoundingClientRect();
          if (a.right > b.left + 1 && a.bottom > b.top && a.top < b.bottom) {
            problems.push({ index, kind: 'title-overlaps-copy' });
          }
        }
        const stats = card.querySelector('.wbm-stats');
        if (stats && stats.scrollWidth - stats.clientWidth > 1) {
          problems.push({ index, kind: 'stats-overflow' });
        }
      });
      return problems;
    });
    assert.deepEqual(clipping, [], '卡片内容不应被裁切或互相重叠：' + JSON.stringify(clipping));

    await page.screenshot({ path: path.join(out, 'market-desktop.png'), fullPage: true });

    // 几何体检：渐变层盖住工具条与第一排卡片之间的过渡，卡片之间不留白缝
    const geometry = await page.evaluate(() => {
      const panel = document.querySelector('#panel-market');
      const toolbar = panel.querySelector('.panel-toolbar');
      const summaryNode = panel.querySelector('.wbm-summary');
      const grid = panel.querySelector('.wbm-grid');
      const firstCard = grid && grid.firstElementChild;
      const cells = firstCard && firstCard.querySelector('.wbm-cells');
      const cell = cells && cells.firstElementChild;
      const box = (node) => {
        const rect = node.getBoundingClientRect();
        return { left: Math.round(rect.left), right: Math.round(rect.right),
                 top: Math.round(rect.top), bottom: Math.round(rect.bottom),
                 height: Math.round(rect.height), width: Math.round(rect.width) };
      };
      return {
        toolbar: box(toolbar),
        summary: summaryNode ? box(summaryNode) : null,
        card: box(firstCard),
        // 第二张卡用来算「两列之间的缝」在哪 —— 渐变连续性就在这条竖线上量
        second: grid.children[1] ? box(grid.children[1]) : null,
        // 真正的「空白」= 上一块内容的下沿到卡片上沿的距离（summary 是内容，不算空白）
        gapToolbarToSummary: summaryNode
          ? Math.round(summaryNode.getBoundingClientRect().top -
                       toolbar.getBoundingClientRect().bottom) : null,
        gapToCards: Math.round(firstCard.getBoundingClientRect().top -
                               summaryNode.getBoundingClientRect().bottom),
        cells: box(cells),
        cell: box(cell),
        panelLayer: box(panel),
        // 柔光现在是**整页一层**（.app-main::before），面板自己不再画渐变
        glowFilter: getComputedStyle(document.querySelector('.app-main'), '::before').filter,
        glowLayerHeight: Math.round(parseFloat(
          getComputedStyle(document.querySelector('.app-main'), '::before').height) || 0),
        chromeBg: getComputedStyle(document.querySelector('.desktop-topbar')).backgroundImage,
        cardBg: getComputedStyle(firstCard).backgroundColor,
      };
    });
    assert(geometry.summary, '卡片上方应有「窗口 / 每格 / 图例」汇总条');
    assert(geometry.gapToolbarToSummary <= 24,
      '工具条与汇总条之间不应留下大段空白，实际 ' + geometry.gapToolbarToSummary + 'px');
    assert(geometry.gapToCards <= 16,
      '汇总条与卡片之间不应留下大段空白，实际 ' + geometry.gapToCards + 'px');
    assert(geometry.cell.width >= 3, '每格至少 3px 才看得见，实际 ' + geometry.cell.width + 'px');
    assert(geometry.cells.width > 150, '状态轴应铺满卡片宽度，实际 ' + geometry.cells.width + 'px');
    // 光晕必须带高斯模糊（方块感就是没有模糊、边界是一条直线造成的）
    assert(/blur\(/.test(geometry.glowFilter), '顶部柔光应使用 blur()，实际 ' + geometry.glowFilter);
    assert(geometry.glowLayerHeight >= 300, '柔光层太矮会露出下边界，实际 ' + geometry.glowLayerHeight + 'px');
    // 顶栏不能是纯白不透明 —— 那会在柔光上方压出一条白带
    assert(/linear-gradient/.test(geometry.chromeBg), '顶栏应是渐变薄纱，实际 ' + geometry.chromeBg);
    if (process.env.WBD_DEBUG) console.log('GEOMETRY', JSON.stringify(geometry));

    // ── 像素级：整页从上到下有没有一道硬边 ────────────────────────────────
    // 做法：把文字隐藏（保留布局）→ 截一张全页图 → 塞回浏览器 canvas 逐点取色。
    // 只测背景，取样点在「两列卡片之间的缝隙」这条竖线上，从**顶栏**一路扫到
    // 第一排卡片下方。判定三条：
    //   1) 顶部确实有色（不是没生效）；
    //   2) 卡片上沿仍有残留（柔光钻进卡片底下，而不是半空断掉）；
    //   3) 相邻取样点之间没有大跳变（跳变 = 看得见的那道缝 / 那条直角边）。
    const hideChrome = await page.addStyleTag({ content:
      '#panel-market .catalog-intro, #panel-market .studio-search, ' +
      '#panel-market .wbm-toolbar, #panel-market .wbm-summary, ' +
      '.topbar-breadcrumb, .topbar-actions, .topbar-subnav { visibility: hidden !important; }' });
    await page.waitForTimeout(150);
    const shot = await page.screenshot({ fullPage: true });
    const seamColumn = await page.evaluate(async (payload) => {
      const img = new Image();
      img.src = payload.url;              // secure-fetch 接管了 fetch，这里走 Image
      await img.decode();
      const canvas = new OffscreenCanvas(img.naturalWidth, img.naturalHeight);
      const ctx = canvas.getContext('2d');
      ctx.drawImage(img, 0, 0);
      const at = (x, y) => {
        const px = Math.round(x);
        const py = Math.round(y);
        if (px < 0 || py < 0 || px >= canvas.width || py >= canvas.height) return null;
        const data = ctx.getImageData(px, py, 1, 1).data;
        return [data[0], data[1], data[2]];
      };
      const gapX = (payload.card.right + payload.second.left) / 2;
      const column = [];
      for (let y = 2; y <= Math.round(payload.card.top + 60); y += 4) {
        column.push({ y, rgb: at(gapX, y) });
      }
      return {
        column,
        pageBg: at(gapX, canvas.height - 30),
        cardTop: at(gapX, payload.card.top + 2),
        header: at(gapX, 40),
        edge: at(gapX, 210),
      };
    }, { url: 'data:image/png;base64,' + shot.toString('base64'), card: geometry.card,
         second: geometry.second || geometry.card, panel: geometry.panelLayer });
    await hideChrome.evaluate((node) => node.remove());

    const diff = (a, b) => (!a || !b) ? 0
      : Math.max(Math.abs(a[0] - b[0]), Math.abs(a[1] - b[1]), Math.abs(a[2] - b[2]));
    const rows = seamColumn.column.filter((row) => row.rgb);
    let worst = { y: 0, jump: 0 };
    for (let i = 1; i < rows.length; i += 1) {
      const jump = diff(rows[i - 1].rgb, rows[i].rgb);
      if (jump > worst.jump) worst = { y: rows[i].y, jump };
    }
    const headerTint = diff(seamColumn.header, seamColumn.pageBg);
    const cardTint = diff(seamColumn.cardTop, seamColumn.pageBg);
    if (process.env.WBD_DEBUG) {
      console.log('SEAM', JSON.stringify({ headerTint, cardTint, worst, pageBg: seamColumn.pageBg,
        header: seamColumn.header, cardTop: seamColumn.cardTop, edge: seamColumn.edge }));
    }
    assert(headerTint >= 8, '顶部（含顶栏）应当有可见的品牌色柔光，实际强度 ' + headerTint);
    assert(cardTint > 0, '柔光应在卡片上沿仍有残留（被卡片盖住），实际 ' + cardTint);
    assert(worst.jump <= 10, '整页在 y=' + worst.y + ' 处出现 ' + worst.jump + ' 的跳变（这就是那道缝）');

    await page.locator('#wbmAvailable').check();
    await page.waitForFunction(() => document.querySelectorAll('.wbm-card').length === 3);
    await page.getByRole('button', { name: '列表' }).click();
    await page.locator('.wbm-table tbody tr').first().waitFor();
    assert.equal(await page.locator('.wbm-table tbody tr').count(), 3, '列表行数');
    assert(await page.locator('.wbm-tl-cell .wbm-cells.is-mini').count() >= 3, '列表也要有状态轴');
    await page.screenshot({ path: path.join(out, 'market-list.png'), fullPage: true });
    await page.locator('#wbmAvailable').uncheck();
    await page.getByRole('button', { name: '卡片' }).click();

    // 只看有故障时段的模型
    await page.locator('#wbmTrouble').check();
    await page.waitForFunction(() => document.querySelectorAll('.wbm-card').length === 2);
    await page.locator('#wbmTrouble').uncheck();
    await page.waitForFunction(() => document.querySelectorAll('.wbm-card').length === 4);

    // 深色 / 窄屏下的广场（状态轴在暗底上最容易糊）
    for (const [name, width, height, dark] of [['dark', 1280, 960, true], ['mobile', 390, 844, false]]) {
      await page.setViewportSize({ width, height });
      await page.evaluate((value) => { document.documentElement.dataset.theme = value ? 'dark' : 'light'; }, dark);
      await page.waitForTimeout(250);
      assert(!(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)),
        'market-' + name + ' 横向溢出');
      await page.screenshot({ path: path.join(out, 'market-' + name + '.png'), fullPage: true });
    }
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.evaluate(() => { document.documentElement.dataset.theme = 'light'; });

    // 回到看板再拍深浅两套主题
    await page.evaluate(() => activateTab('stats'));
    await page.locator('#panel-stats:not(.hidden)').waitFor();

    // 深色 + 窄屏
    for (const [name, width, height, dark] of [['dark', 1280, 960, true], ['mobile', 390, 844, false]]) {
      await page.setViewportSize({ width, height });
      await page.evaluate((value) => { document.documentElement.dataset.theme = value ? 'dark' : 'light'; }, dark);
      await page.evaluate(() => window.WBStatsDashboard.repaint());
      await page.waitForTimeout(250);
      assert(!(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)), name + ' 横向溢出');
      await page.screenshot({ path: path.join(out, 'dashboard-' + name + '.png'), fullPage: true });
    }

    assert.deepEqual(errors, [], '页面不应有脚本错误');
    console.log(JSON.stringify({ passed: true, kpis: 7, bars, screenshots: out }));
  } finally {
    await browser.close();
    server.close();
  }
})().catch((error) => { console.error(error); process.exit(1); });