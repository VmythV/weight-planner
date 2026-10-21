'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const STATUS = {
  ahead: { text: '领先计划', cls: 'good' },
  on_track: { text: '按计划进行', cls: 'good' },
  slightly_behind: { text: '略微落后', cls: 'warn' },
  behind: { text: '落后计划', cls: 'bad' },
  reached: { text: '已达成目标', cls: 'good' },
  not_started: { text: '计划未开始', cls: '' },
  not_applicable: { text: '维持 / 增重', cls: '' },
};
const ALERT_ICON = { warn: 'i-alert', danger: 'i-alert', info: 'i-info', success: 'i-check' };
const SOURCE = { manual: '手动', import: '导入', esp32: '体重秤' };
const WEEKDAY = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
const KCAL_PER_KG = 7700;
const HISTORY_SHORT = 10;
const HISTORY_LONG = 500;
const STEP = 0.05;             // 体重选择器的最小步长
const PX_PER_KG = 80;          // 刻度尺上 1 kg 的宽度（每 0.1 kg 8px）
const MORNING = '07:30';
const REFRESH_MS = 60_000;     // 体重秤自动上报后，页面一分钟内自动刷新

let weightChart = null;
let calChart = null;
let deltaChart = null;
let lastDash = null;
let lastHistory = [];
let historyLimit = HISTORY_SHORT;
let range = loadPref('range', '90');

// ---------- 工具 ----------

function loadPref(key, fallback) {
  try { return localStorage.getItem(`wp.${key}`) ?? fallback; } catch { return fallback; }
}
function savePref(key, value) {
  try { localStorage.setItem(`wp.${key}`, value); } catch { /* 隐私模式下忽略 */ }
}

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));
const fmt = (v, digits = 1) => (v == null || Number.isNaN(v) ? '—' : Number(v).toFixed(digits));
const kcal = (v) => (v == null ? '—' : Math.round(v).toLocaleString('zh-CN'));
const signed = (v, digits = 2) => (v == null ? '—' : `${v > 0 ? '+' : v < 0 ? '−' : '±'}${Math.abs(v).toFixed(digits)}`);
const roundStep = (v) => Math.round(v / STEP) * STEP;

const pad2 = (n) => String(n).padStart(2, '0');
function localIso(d) {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}
const todayIso = () => localIso(new Date()).slice(0, 10);
function isoAddDays(iso, days) {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}
function daysBetween(a, b) {
  return Math.round((new Date(`${b}T00:00:00Z`) - new Date(`${a}T00:00:00Z`)) / 86_400_000);
}
function weekday(iso) {
  return WEEKDAY[new Date(`${iso.slice(0, 10)}T00:00:00Z`).getUTCDay()];
}
function humanDay(iso) {
  const day = iso.slice(0, 10);
  const diff = daysBetween(day, todayIso());
  if (diff === 0) return '今天';
  if (diff === 1) return '昨天';
  if (diff === 2) return '前天';
  return `${Number(day.slice(5, 7))}月${Number(day.slice(8, 10))}日`;
}

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  if (res.status === 401) {
    // 登录过期或改了密码，回到登录页
    location.replace('/login');
    throw new Error('需要重新登录');
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      detail = Array.isArray(body.detail)
        ? body.detail.map((d) => String(d.msg).replace(/^Value error, /, '')).join('；')
        : String(body.detail ?? detail);
    } catch { /* 保留状态码描述 */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

function setMsg(el, text, kind = 'ok') {
  el.textContent = text;
  el.className = `form-msg ${kind}`;
}

let toastTimer = null;
function toast(text) {
  const el = $('#toast');
  el.innerHTML = `<svg><use href="#i-check"/></svg><span>${esc(text)}</span>`;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2200);
}

// ---------- 分析 ----------

/** 返回 dates 中 day 当天或之前最近一个非空值的下标。 */
function lastValueIndex(values, dates, day) {
  let i = dates.indexOf(day);
  if (i < 0) i = day > dates[dates.length - 1] ? dates.length - 1 : -1;
  for (; i >= 0; i -= 1) if (values[i] != null) return i;
  return -1;
}

/** 每个有值的日期和上一个有值的日期之差；prev 是对比日的下标（漏称时会隔好几天）。 */
function dailyDeltas(values) {
  const delta = values.map(() => null);
  const prev = values.map(() => null);
  let last = -1;
  values.forEach((v, i) => {
    if (v == null) return;
    if (last >= 0) {
      delta[i] = +(v - values[last]).toFixed(2);
      prev[i] = last;
    }
    last = i;
  });
  return { delta, prev };
}

// ---------- 渲染：概览 ----------

function renderAlerts(dash) {
  const list = dash.ready ? dash.warnings : [];
  $('#alerts').innerHTML = list.map((w) => `
    <div class="alert ${esc(w.level)}">
      <svg><use href="#${ALERT_ICON[w.level] ?? 'i-info'}"/></svg>
      <div>${esc(w.text)}</div>
    </div>`).join('');
}

function renderEmpty(dash) {
  const box = $('#emptyState');
  box.hidden = dash.ready;
  if (dash.ready) return;
  const noProfile = dash.reason === 'no_profile';
  $('#emptyTitle').textContent = noProfile ? '先设定你的目标' : '记录第一次体重';
  $('#emptyText').textContent = noProfile
    ? '填写身高、目标体重和日期后，会生成每天的摄入建议和计划曲线。'
    : '有了第一条称重，就能画出计划曲线和趋势体重。';
  const btn = $('#emptyAction');
  btn.textContent = noProfile ? '填写计划设置' : '记录体重';
  btn.onclick = noProfile ? openSettings : () => openWeigh();
}

function renderHero(dash) {
  const c = dash.card;
  const p = dash.plan;
  const st = STATUS[c.status] ?? STATUS.not_applicable;
  $('#heroWeight').textContent = fmt(c.trend_weight);
  const pill = $('#heroStatus');
  pill.className = `pill ${st.cls}`;
  pill.textContent = st.text;

  let diff = '';
  if (c.diff != null && c.plan_weight_today != null) {
    const d = Math.abs(c.diff).toFixed(2);
    diff = c.diff > 0 ? `比今日计划 ${fmt(c.plan_weight_today)} kg 高 ${d} kg` : `比今日计划 ${fmt(c.plan_weight_today)} kg 低 ${d} kg`;
  }
  $('#heroDiff').textContent = diff;

  const latest = lastHistory.find((r) => r.measured_at.slice(0, 10) === c.latest_date);
  $('#heroLatest').textContent = c.latest_weight == null ? ''
    : `最近称重 ${fmt(c.latest_weight, 2)} kg · ${humanDay(c.latest_date)}${latest ? ` ${latest.measured_at.slice(11, 16)}` : ''}`;

  const pct = Math.max(0, Math.min(100, c.progress_pct ?? 0));
  $('#progressPct').textContent = c.progress_pct == null ? '' : `${fmt(pct)}%`;
  $('#progressFill').style.width = `${pct}%`;
  const span = p.start_weight - p.target_weight;
  const planPct = span && c.plan_weight_today != null
    ? Math.max(0, Math.min(100, ((p.start_weight - c.plan_weight_today) / span) * 100)) : null;
  const marker = $('#progressPlan');
  marker.hidden = planPct == null;
  if (planPct != null) marker.style.left = `${planPct}%`;
  $('#progressStart').textContent = `起点 ${fmt(p.start_weight)} kg`;
  $('#progressTarget').textContent = `目标 ${fmt(p.target_weight)} kg · ${p.target_date}`;

  $('#factLost').textContent = `${fmt(c.lost_kg)} kg`;
  $('#factRemain').textContent = `${fmt(c.remaining_kg)} kg`;
  $('#factDays').textContent = `${c.days_left} 天`;

  let outlook = '竖线是今天计划应到的位置。';
  if (c.reached) outlook = '已经达到目标，接下来按维持热量巩固。';
  else if (c.ended) outlook = '目标日已过，可以在「计划设置」里制定新计划。';
  else if (c.projected_reach_date) {
    const late = daysBetween(p.target_date, c.projected_reach_date);
    outlook = `按当前建议摄入，预计 ${c.projected_reach_date} 达到目标`
      + (late > 0 ? `，比目标日晚 ${late} 天。` : '。')
      + '竖线是今天计划应到的位置。';
  }
  $('#heroOutlook').textContent = outlook;
}

function renderKpis(dash) {
  const c = dash.card;
  const p = dash.plan;
  const lastCheckin = dash.checkins[dash.checkins.length - 1];
  const atFloor = lastCheckin && !lastCheckin.reached && lastCheckin.recommended_intake > lastCheckin.required_intake;
  const theory = (c.deficit * 7) / KCAL_PER_KG;
  const act = dash.action?.applicable ? dash.action.actual : null;

  let rateTile = `
    <div class="eyebrow">实际速度</div>
    <div class="kpi-value">—</div>
    <div class="kpi-sub">称重满 7 天后显示</div>`;
  if (act) {
    const implied = (-act.rate * KCAL_PER_KG) / 7;
    const gapNote = c.deficit > 0 && implied < c.deficit * 0.6
      ? `<div class="kpi-sub kpi-note warn"><svg><use href="#i-alert"/></svg>反推缺口约 ${kcal(Math.max(implied, 0))} kcal/天，明显低于估算</div>`
      : `<div class="kpi-sub">反推实际缺口约 ${kcal(Math.max(implied, 0))} kcal/天</div>`;
    rateTile = `
      <div class="eyebrow">实际速度 · 近 ${act.window_days} 天</div>
      <div class="kpi-value">${signed(act.rate)}<small>kg/周</small></div>
      <div class="kpi-sub">按建议摄入应为 ${signed(dash.action.expected_rate)} kg/周</div>
      ${gapNote}`;
  }

  const exercise = c.burn_exercise ? ` + 运动 ${kcal(c.burn_exercise)}` : '';
  $('#kpis').innerHTML = `
    <div class="card kpi">
      <div class="eyebrow">今天可以摄入</div>
      <div class="kpi-value">${kcal(c.intake)}<small>kcal</small></div>
      <div class="kpi-sub">蛋白质 ${c.protein_g} g · 脂肪 ${c.fat_g} g · 碳水 ${c.carbs_g} g</div>
      ${atFloor ? `<div class="kpi-sub kpi-note warn"><svg><use href="#i-alert"/></svg>已按安全下限 ${kcal(p.intake_floor)} kcal</div>` : ''}
    </div>
    <div class="card kpi">
      <div class="eyebrow">今天预计消耗</div>
      <div class="kpi-value">${kcal(c.burn_total)}<small>kcal</small></div>
      <div class="kpi-sub">基础代谢 ${kcal(c.burn_bmr)} + 日常活动 ${kcal(c.burn_activity)}${exercise}</div>
      ${c.exercise_steps ? `<div class="kpi-sub">运动约等于快走 ${c.exercise_steps.toLocaleString('zh-CN')} 步</div>` : ''}
    </div>
    <div class="card kpi">
      <div class="eyebrow">热量缺口</div>
      <div class="kpi-value">${kcal(c.deficit)}<small>kcal</small></div>
      <div class="kpi-sub">理论上每周 ${signed(-theory)} kg</div>
    </div>
    <div class="card kpi">${rateTile}</div>`;
}

// ---------- 渲染：图表 ----------

function themeColors() {
  const cs = getComputedStyle(document.documentElement);
  const v = (name) => cs.getPropertyValue(name).trim();
  return {
    ink: v('--ink'), ink2: v('--ink-2'), ink3: v('--ink-3'), grid: v('--hairline'), axis: v('--axis'),
    surface: v('--surface'), trend: v('--s-trend'), proj: v('--s-proj'), plan: v('--s-plan'),
    dots: v('--s-dots'), band: v('--s-band'), gap: v('--s-gap'),
    down: v('--good'), up: v('--critical'),
  };
}

function zoomWindow(dash) {
  const dates = dash.series.dates;
  const first = dates[0];
  const last = dates[dates.length - 1];
  if (range === 'all') return { startValue: first, endValue: last };
  const days = Number(range);
  const clamp = (d) => (d < first ? first : d > last ? last : d);
  return {
    startValue: clamp(isoAddDays(dash.today, -days)),
    endValue: clamp(isoAddDays(dash.today, Math.round(days / 3))),
  };
}

function tooltipHtml(dash, i) {
  const s = dash.series;
  const rows = [
    ['当天称重', s.actual[i], 'kg'],
    ['趋势体重', s.trend[i], 'kg'],
    ['计划体重', s.plan[i], 'kg'],
    ['预测体重', s.projection[i], 'kg'],
    ['建议摄入', s.intake[i], 'kcal'],
    ['预计消耗', s.burn[i], 'kcal'],
    ['热量缺口', s.deficit[i], 'kcal'],
  ].filter((r) => r[1] != null);
  const lines = rows.map(([k, v, u]) => `<span style="opacity:.7">${k}</span>　${u === 'kg' ? fmt(v, 2) : kcal(v)} ${u}`);
  return [`<b>${esc(s.dates[i])} ${weekday(s.dates[i])}</b>`, ...lines].join('<br>');
}

function baseOption(dash, col) {
  const narrow = $('#weightChart').clientWidth < 520; // 窄屏时图例会折成两行
  return {
    animation: false,
    textStyle: { color: col.ink2, fontFamily: 'system-ui, -apple-system, "PingFang SC", sans-serif' },
    grid: { left: 8, right: narrow ? 44 : 64, top: narrow ? 60 : 40, bottom: 8, containLabel: true },
    legend: {
      top: 0, left: 0, itemWidth: 16, itemHeight: 8, itemGap: 16,
      textStyle: { color: col.ink2, fontSize: 12 },
    },
    tooltip: {
      trigger: 'axis',
      confine: true,
      backgroundColor: col.surface,
      borderColor: col.grid,
      padding: [8, 12],
      textStyle: { color: col.ink, fontSize: 12 },
      axisPointer: { type: 'line', lineStyle: { color: col.axis } },
      formatter: (params) => tooltipHtml(dash, params[0].dataIndex),
    },
    xAxis: {
      type: 'category',
      data: dash.series.dates,
      boundaryGap: false,
      axisTick: { show: false },
      axisLine: { lineStyle: { color: col.axis } },
      axisLabel: { color: col.ink3, fontSize: 11, hideOverlap: true, formatter: (v) => `${Number(v.slice(5, 7))}/${Number(v.slice(8))}` },
    },
    yAxis: {
      type: 'value',
      scale: true,
      splitNumber: 4,
      axisLabel: { color: col.ink3, fontSize: 11 },
      splitLine: { lineStyle: { color: col.grid } },
    },
    // 只用来控制显示范围（由上方的 30 天 / 90 天 / 全程切换），不拦截页面滚动
    dataZoom: [{
      type: 'inside', ...zoomWindow(dash),
      zoomLock: true, zoomOnMouseWheel: false, moveOnMouseMove: false, moveOnMouseWheel: false,
    }],
  };
}

function weightOption(dash, col) {
  const s = dash.series;
  const base = baseOption(dash, col);
  const bandWidth = s.band_high.map((h, i) => (h == null ? null : +(h - s.band_low[i]).toFixed(2)));
  const todayIdx = lastValueIndex(s.trend, s.dates, dash.today);
  const inRange = s.dates.includes(dash.today);
  return {
    ...base,
    legend: { ...base.legend, data: ['趋势体重', '每日称重', '计划曲线', '当前预测'] },
    series: [
      {
        name: 'band-base', type: 'line', data: s.band_low, stack: 'band',
        symbol: 'none', lineStyle: { opacity: 0 }, silent: true, tooltip: { show: false },
      },
      {
        name: '容差带', type: 'line', data: bandWidth, stack: 'band',
        symbol: 'none', lineStyle: { opacity: 0 }, areaStyle: { color: col.band }, silent: true,
      },
      {
        name: '计划曲线', type: 'line', data: s.plan, smooth: true, symbol: 'none', z: 2,
        lineStyle: { type: [6, 4], width: 1.5, color: col.plan }, itemStyle: { color: col.plan },
        markLine: inRange ? {
          symbol: 'none', silent: true, data: [{ xAxis: dash.today }],
          label: { formatter: '今天', color: col.ink3, fontSize: 11, position: 'end' },
          lineStyle: { color: col.axis, type: 'solid', width: 1 },
        } : undefined,
        markPoint: {
          symbol: 'circle', symbolSize: 9, silent: true,
          itemStyle: { color: col.surface, borderColor: col.plan, borderWidth: 2 },
          label: {
            show: true, position: 'top', distance: 6, color: col.ink2, fontSize: 11,
            formatter: `目标 ${fmt(dash.plan.target_weight)} kg`,
          },
          data: [{ coord: [dash.plan.target_date, dash.plan.target_weight] }],
        },
      },
      {
        name: '每日称重', type: 'scatter', data: s.actual, symbolSize: 8, z: 3,
        itemStyle: { color: col.dots, borderColor: col.surface, borderWidth: 1.5 },
      },
      {
        name: '趋势体重', type: 'line', data: s.trend, smooth: true, connectNulls: true, symbol: 'none', z: 4,
        lineStyle: { width: 2.5, color: col.trend }, itemStyle: { color: col.trend },
        markPoint: todayIdx < 0 ? undefined : {
          symbol: 'circle', symbolSize: 10, silent: true,
          itemStyle: { color: col.trend, borderColor: col.surface, borderWidth: 2 },
          label: {
            show: true, position: 'right', distance: 6, color: col.ink, fontSize: 12, fontWeight: 600,
            formatter: fmt(s.trend[todayIdx]),
          },
          data: [{ coord: [s.dates[todayIdx], s.trend[todayIdx]] }],
        },
      },
      {
        name: '当前预测', type: 'line', data: s.projection, smooth: true, symbol: 'none', z: 3,
        lineStyle: { type: [2, 4], width: 2, color: col.proj }, itemStyle: { color: col.proj },
      },
    ],
  };
}

function calorieOption(dash, col) {
  const s = dash.series;
  const base = baseOption(dash, col);
  const gap = s.burn.map((b, i) => (b == null || s.intake[i] == null ? null : Math.max(b - s.intake[i], 0)));
  return {
    ...base,
    legend: {
      ...base.legend,
      data: ['建议摄入', '预计消耗', { name: '热量缺口', icon: 'rect' }],
    },
    yAxis: {
      ...base.yAxis,
      // 留出下方空间，避免建议摄入线贴着坐标轴
      min: (v) => Math.max(0, Math.floor((v.min - 400) / 500) * 500),
      axisLabel: { ...base.yAxis.axisLabel, formatter: (v) => v.toLocaleString('zh-CN') },
    },
    series: [
      {
        name: '建议摄入', type: 'line', data: s.intake, stack: 'cal', symbol: 'none', z: 3,
        lineStyle: { width: 2, color: col.trend }, itemStyle: { color: col.trend },
      },
      {
        name: '热量缺口', type: 'line', data: gap, stack: 'cal', symbol: 'none', silent: true,
        lineStyle: { opacity: 0 }, areaStyle: { color: col.gap }, itemStyle: { color: col.gap },
      },
      {
        name: '预计消耗', type: 'line', data: s.burn, smooth: true, symbol: 'none', z: 3,
        lineStyle: { width: 2, color: col.proj }, itemStyle: { color: col.proj },
      },
    ],
  };
}

/**
 * 折线在 0 以上用上升色、以下用下降色：用整张图的像素坐标铺渐变，在 y=0 的位置硬切换。
 * 按折线包围盒铺会错位，因为缩放后窗口外的点不参与绘制，包围盒随显示范围变化。
 */
function signGradient(chart, col) {
  const height = chart.getHeight();
  const zero = Math.min(Math.max(chart.convertToPixel({ yAxisIndex: 0 }, 0) / height, 0), 1);
  return new echarts.graphic.LinearGradient(0, 0, 0, height, [
    { offset: 0, color: col.up }, { offset: zero, color: col.up },
    { offset: zero, color: col.down }, { offset: 1, color: col.down },
  ], true);
}

function deltaOption(dash, col) {
  const s = dash.series;
  const base = baseOption(dash, col);
  const actual = dailyDeltas(s.actual);
  const trend = dailyDeltas(s.trend);
  // 计划曲线逐日的差值；只算计划期内（容差带有值的日子）
  const planDelta = s.plan.map((v, i) => (
    i > 0 && s.band_low[i] != null && s.band_low[i - 1] != null ? +(v - s.plan[i - 1]).toFixed(2) : null
  ));
  const signColor = (v) => (v > 0 ? col.up : col.down);
  const deltaLine = (label, d, i) => {
    if (d.delta[i] == null) return null;
    const gap = i - d.prev[i];
    const since = gap === 1 ? '' : `（较 ${s.dates[d.prev[i]].slice(5)}，隔 ${gap} 天）`;
    return `<span style="opacity:.7">${label}</span>　<span style="color:${signColor(d.delta[i])}">${signed(d.delta[i])}</span> kg${since}`;
  };
  return {
    ...base,
    legend: { show: false },
    grid: { ...base.grid, top: 16 },
    tooltip: {
      ...base.tooltip,
      formatter: (params) => {
        const i = params[0].dataIndex;
        const lines = [deltaLine('每日称重', actual, i), deltaLine('趋势体重', trend, i)].filter(Boolean);
        if (planDelta[i] != null) lines.push(`<span style="opacity:.7">计划</span>　${signed(planDelta[i])} kg`);
        return [`<b>${esc(s.dates[i])} ${weekday(s.dates[i])}</b>`, ...(lines.length ? lines : ['<span style="opacity:.7">没有可对比的称重</span>'])].join('<br>');
      },
    },
    yAxis: {
      ...base.yAxis,
      scale: false,
      axisLabel: { ...base.yAxis.axisLabel, formatter: (v) => (v > 0 ? `+${v}` : `${v}`) },
    },
    series: [
      {
        name: '每日称重', type: 'bar', data: actual.delta, barMaxWidth: 10, z: 2,
        itemStyle: { color: (p) => signColor(p.value), opacity: 0.45, borderRadius: 2 },
        markLine: {
          symbol: 'none', silent: true, label: { show: false },
          data: [{ yAxis: 0 }], lineStyle: { color: col.axis, type: 'solid', width: 1 },
        },
      },
      {
        name: '趋势体重', type: 'line', data: trend.delta, connectNulls: true, symbol: 'none', z: 3,
        lineStyle: { width: 2.5 }, // 颜色在渲染后按 0 的像素位置设置，见 signGradient
      },
      {
        name: '计划', type: 'line', data: planDelta, symbol: 'none', z: 2, silent: true,
        lineStyle: { type: [6, 4], width: 1.5, color: col.plan },
      },
    ],
  };
}

function renderCharts(dash) {
  if (!dash || !dash.ready) return;
  const col = themeColors();
  weightChart ??= echarts.init($('#weightChart'));
  deltaChart ??= echarts.init($('#deltaChart'));
  calChart ??= echarts.init($('#calChart'));
  weightChart.setOption(weightOption(dash, col), true);
  deltaChart.setOption(deltaOption(dash, col), true);
  deltaChart.setOption({ series: [{}, { lineStyle: { color: signGradient(deltaChart, col) } }] });
  calChart.setOption(calorieOption(dash, col), true);
  for (const c of [weightChart, deltaChart, calChart]) c.group = 'weight-planner';
  echarts.connect('weight-planner');
}

function renderRange() {
  for (const b of $$('#rangeSeg button')) b.setAttribute('aria-checked', String(b.dataset.range === range));
}

// ---------- 渲染：记录与校准 ----------

function renderHistory(rows) {
  const counted = new Set();
  const seenDays = new Set();
  // 列表按时间倒序，每天最后出现的一条就是当天最早的称重
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    const day = rows[i].measured_at.slice(0, 10);
    if (!seenDays.has(day)) { seenDays.add(day); counted.add(rows[i].id); }
  }
  const countedRows = rows.filter((r) => counted.has(r.id));
  const prevOf = new Map(countedRows.map((r, i) => [r.id, countedRows[i + 1]]));

  $('#history').innerHTML = rows.length ? rows.map((r) => {
    const day = r.measured_at.slice(0, 10);
    const isCounted = counted.has(r.id);
    const prev = prevOf.get(r.id);
    const delta = isCounted && prev ? r.weight_kg - prev.weight_kg : null;
    const extras = [];
    if (r.heart_rate) extras.push(`心率 ${r.heart_rate}`);
    if (r.impedance_low) extras.push(`阻抗 ${Math.round(r.impedance_low)}${r.impedance_high ? `/${Math.round(r.impedance_high)}` : ''} Ω`);
    if (!isCounted) extras.push('当天非首次，不计入');
    return `<li><button type="button" class="history-row${isCounted ? '' : ' h-ignored'}" data-id="${r.id}">
      <span class="h-date">${Number(day.slice(5, 7))}/${Number(day.slice(8))}<small>${weekday(day)}</small></span>
      <span class="h-meta"><span class="h-source">${esc(SOURCE[r.source] ?? r.source)}</span>${esc(r.measured_at.slice(11, 16))}${extras.length ? ` · ${esc(extras.join(' · '))}` : ''}</span>
      <span class="h-delta">${delta == null ? '' : signed(delta)}</span>
      <span class="h-weight">${fmt(r.weight_kg, 2)}<small>kg</small></span>
    </button></li>`;
  }).join('') : '<li class="history-empty">还没有记录</li>';

  const more = $('#moreHistory');
  more.hidden = historyLimit === HISTORY_SHORT && rows.length < HISTORY_SHORT;
  more.textContent = historyLimit === HISTORY_SHORT ? '显示全部记录' : '收起';
}

function renderCheckins(dash) {
  const rows = dash.ready ? dash.checkins : [];
  $('#checkinPanel').hidden = rows.length === 0;
  $('#checkins tbody').innerHTML = rows.slice().reverse().map((c) => {
    let note = '';
    if (c.reached) note = '<small>维持</small>';
    else if (c.recommended_intake > c.required_intake) note = '<small>下限</small>';
    return `<tr>
      <td>${esc(c.day.slice(5))} <small>${weekday(c.day)}</small></td>
      <td class="num">${fmt(c.trend_weight, 2)}</td>
      <td class="num">${kcal(c.recommended_intake)}${note}</td>
      <td class="num">${kcal(c.required_intake)}</td>
    </tr>`;
  }).join('');
}

function renderPlanSummary(dash) {
  const box = $('#planSummary');
  if (!dash.ready) { box.innerHTML = ''; return; }
  const p = dash.plan;
  const items = [
    `起始 ${fmt(p.start_weight, 2)} kg → 目标 ${fmt(p.target_weight)} kg，共 ${p.days} 天，目标 BMI ${fmt(p.target_bmi)}`,
    `按期达成需每天摄入约 ${kcal(p.intake)} kcal（安全下限 ${kcal(p.intake_floor)} kcal）`,
  ];
  if (p.losing) items.push(`开始阶段每周约减体重的 ${fmt(p.weekly_loss_pct, 2)}%（建议不超过 1%）`);
  if (p.suggested_date) items.push(`按安全速度，建议目标日期不早于 ${p.suggested_date}`);
  box.innerHTML = `<ul>${items.map((t) => `<li>${esc(t)}</li>`).join('')}</ul>`;
}

function renderProfile(profile) {
  const form = $('#profileForm');
  const values = profile ?? {
    sex: 'male', activity: 'light', plan_start: todayIso(),
    target_date: isoAddDays(todayIso(), 90), exercise_kcal: 0,
  };
  for (const el of form.elements) {
    if (el.name && el.name in values) el.value = values[el.name] ?? '';
  }
  setRadio('sex', values.sex ?? 'male');
}

function setRadio(name, value) {
  const group = $(`[data-radio="${name}"]`);
  group.dataset.value = value;
  for (const b of $$('button', group)) b.setAttribute('aria-checked', String(b.dataset.value === value));
}

async function refresh() {
  const [dash, profile, rows] = await Promise.all([
    api('/api/dashboard'),
    api('/api/profile'),
    api(`/api/weighins?limit=${historyLimit}`),
  ]);
  lastDash = dash;
  lastHistory = rows;
  $('#todayLabel').textContent = `${Number(dash.today.slice(5, 7))}月${Number(dash.today.slice(8))}日 ${weekday(dash.today)}`;
  renderProfile(profile);
  renderHistory(rows);
  renderAlerts(dash);
  renderEmpty(dash);
  $('#dashboard').hidden = !dash.ready;
  if (dash.ready) {
    renderHero(dash);
    renderKpis(dash);
    renderAction(dash);
    renderCharts(dash);
  }
  renderCheckins(dash);
  renderPlanSummary(dash);
  if (view === 'body') await loadBody();
  if (view === 'report') await loadReport(reportDay);
}


// ---------- 行动方案 ----------

function renderAction(dash) {
  const a = dash.action;
  const card = $('#actionCard');
  card.hidden = !a?.applicable;
  if (card.hidden) return;
  const p = dash.plan;
  const k = a.keep_date;
  const lateFloor = a.floor_only.reach_date ? daysBetween(p.target_date, a.floor_only.reach_date) : null;
  const lateSafe = a.safe.reach_date ? daysBetween(p.target_date, a.safe.reach_date) : null;
  // 推荐：按期方案可行且不过快时推荐保持日期，否则推荐安全速度
  const recommendKeep = k.feasible && !k.too_fast;
  const late = (n) => (n == null ? '' : n > 0 ? `，比目标日晚 ${n} 天` : n < 0 ? `，比目标日早 ${-n} 天` : '');

  const keepMain = k.extra_exercise > 0
    ? `${kcal(k.intake)}<small>kcal</small> + 运动 ${kcal(k.extra_exercise)}<small>kcal</small>`
    : `${kcal(k.intake)}<small>kcal / 天</small>`;
  const keepText = k.extra_exercise > 0
    ? `吃到安全下限，再每天额外运动约快走 ${k.minutes} 分钟（${k.steps.toLocaleString('zh-CN')} 步）`
    : '只靠控制饮食即可按期达成';
  const keepFlags = [];
  if (k.too_fast) keepFlags.push(`开始阶段每周约 ${signed(-k.weekly_loss_kg)} kg（${fmt(k.weekly_loss_pct, 2)}%），超过每周 1% 的安全速度`);
  if (!k.feasible && k.extra_exercise > 0) keepFlags.push('每天额外运动量较大，长期很难坚持');

  const act = a.actual;
  let reality = '';
  if (act) {
    const pace = act.reach_date ? `按这个速度要到 <b>${act.reach_date}</b> 才能达到目标` : '按这个速度短期内无法达到目标';
    reality = `
      <div><b>实际执行：</b>近 ${act.window_days} 天趋势体重每周 ${signed(act.rate)} kg${act.since_start_rate != null ? `（计划开始以来 ${signed(act.since_start_rate)} kg/周）` : ''}，${pace}。</div>
      ${act.gap_kcal > 150 ? `<div>按建议摄入，模型预计每周应减 ${fmt(-a.expected_rate, 2)} kg，实际少了约 <b>${kcal(act.gap_kcal)} kcal/天</b> 的缺口：要么实际吃得比建议多，要么日常活动消耗被高估了。</div>` : '<div>实际速度和模型预计基本一致，继续保持。</div>'}`;
  }

  const todo = [`每天摄入不超过 <b>${kcal(recommendKeep ? k.intake : Math.max(k.intake, dash.card.intake))} kcal</b>，蛋白质至少 ${a.protein_g} g，优先保证蛋白质和蔬菜`];
  if (recommendKeep && k.extra_exercise > 0) todo.push(`每天额外快走 ${k.minutes} 分钟左右（约 ${k.steps.toLocaleString('zh-CN')} 步，消耗 ${kcal(k.extra_exercise)} kcal）`);
  if (act && act.gap_kcal > 300) {
    todo.push('先连续 1–2 周如实记录吃了什么，确认实际摄入是否在建议范围内');
    todo.push('如果饮食确实达标但体重仍不降，把「日常活动水平」调低一档，让消耗估算更接近实际');
  }
  if (!recommendKeep && a.safe.reach_date) todo.push(`在「计划设置」里把目标日期改到 <b>${a.safe.reach_date}</b> 或更晚，按安全速度减`);
  todo.push('每天早上起床、如厕后空腹称一次，光脚站上体重秤，数据会自动记录');

  card.innerHTML = `
    <div class="action-head">
      <svg><use href="#i-target"/></svg>
      <div>
        <h2>怎样在 ${esc(p.target_date)} 前减到 ${fmt(p.target_weight)} kg</h2>
        <p>还需减 ${fmt(a.remaining_kg)} kg，剩 ${a.days_left} 天，平均每周 ${signed(a.avg_rate)} kg。当前估算每天消耗 ${kcal(a.burn)} kcal。</p>
      </div>
    </div>
    <div class="options">
      <div class="option${recommendKeep ? ' recommended' : ''}">
        <div class="option-title"><span>方案一 · 保持目标日期</span>${recommendKeep ? '<span class="option-tag">推荐</span>' : ''}</div>
        <div class="option-main">${keepMain}</div>
        <div class="option-text">${keepText}</div>
        ${keepFlags.map((f) => `<div class="option-flag"><svg><use href="#i-alert"/></svg>${esc(f)}</div>`).join('')}
      </div>
      <div class="option">
        <div class="option-title"><span>方案二 · 只按下限吃，不加运动</span></div>
        <div class="option-main">${a.floor_only.reach_date ?? '无法达到'}</div>
        <div class="option-text">每天 ${kcal(a.floor_only.intake)} kcal${late(lateFloor)}</div>
      </div>
      <div class="option${recommendKeep ? '' : ' recommended'}">
        <div class="option-title"><span>方案三 · 按安全速度（每周 ≤ 1%）</span>${recommendKeep ? '' : '<span class="option-tag">推荐</span>'}</div>
        <div class="option-main">${a.safe.reach_date ?? '—'}</div>
        <div class="option-text">${a.safe.intake ? `每天约 ${kcal(a.safe.intake)} kcal，更容易坚持${late(lateSafe)}` : ''}</div>
      </div>
    </div>
    ${reality ? `<div class="reality">${reality}</div>` : ''}
    <div class="todo-title">接下来每天要做的</div>
    <ul class="todo">${todo.map((t) => `<li><span>${t}</span></li>`).join('')}</ul>`;
}

// ---------- 标签页 ----------

let view = 'overview';
let reportDay = null;
let bodyData = null;
let bodySource = loadPref('bodySource', 'bia');
let bmiChart = null;
let fatChart = null;
let reportChart = null;

function parseHash() {
  const [name, arg] = location.hash.replace(/^#/, '').split('/');
  return { name: ['overview', 'body', 'report', 'training'].includes(name) ? name : 'overview', arg: arg || null };
}

async function showView() {
  const { name, arg } = parseHash();
  view = name;
  for (const el of $$('[data-view]')) el.hidden = el.dataset.view !== name;
  for (const a of $$('#tabs a')) {
    if (a.dataset.tab === name) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
  try {
    if (name === 'overview') { weightChart?.resize(); deltaChart?.resize(); calChart?.resize(); }
    if (name === 'body') await loadBody();
    if (name === 'report') await loadReport(arg);
    if (name === 'training') await loadTraining(arg);
  } catch (err) {
    toast(`加载失败：${err.message}`);
  }
}

window.addEventListener('hashchange', showView);

// ---------- 指标量表 ----------

const LEVEL_PILL = { good: 'good', warn: 'warn', bad: 'bad', low: '' };

/** 分段量表：bands 是分界值，classes / labels 比分界多一个。 */
function gauge({ name, value, unit = '', digits = 1, bands, classes, labels, min, max, note = '' }) {
  if (value == null) return '';
  const level = bands ? bands.filter((b) => value >= b).length : null;
  const pos = (v) => Math.max(0, Math.min(100, ((v - min) / (max - min)) * 100));
  let bar = '';
  if (bands) {
    const edges = [min, ...bands, max];
    bar = `
      <div class="gauge-bar" role="img" aria-label="${esc(`${name} ${fmt(value, digits)}${unit}，${labels[level]}`)}">
        ${classes.map((c, i) => `<span class="${c}" style="flex:${edges[i + 1] - edges[i]}"></span>`).join('')}
        <i class="gauge-marker" style="left:${pos(value)}%"></i>
      </div>
      <div class="gauge-ticks">${bands.map((b) => `<span style="left:${pos(b)}%">${b}</span>`).join('')}</div>`;
  }
  const pill = level == null ? '' : `<span class="pill ${LEVEL_PILL[classes[level]]}">${esc(labels[level])}</span>`;
  return `
    <div class="gauge${bands ? '' : ' plain'}">
      <div class="gauge-head"><span class="gauge-name">${esc(name)}</span>${pill}</div>
      <div class="gauge-value">${fmt(value, digits)}<small>${esc(unit)}</small></div>
      ${bar}
      ${note ? `<div class="gauge-note">${note}</div>` : ''}
    </div>`;
}

function compositionGauges(c, { bmrMifflin = null, heartRate = null, target = null } = {}) {
  const items = [
    gauge({
      name: 'BMI', value: c.bmi, bands: c.bmi_bands, classes: ['warn', 'good', 'warn', 'bad'],
      labels: c.bmi_labels, min: 15, max: 40,
      note: `健康体重 ${fmt(c.healthy_weight[0])}–${fmt(c.healthy_weight[1])} kg，理想 ${fmt(c.ideal_weight)} kg`,
    }),
    gauge({
      name: c.method === 'bia' ? '体脂率 · 体重秤测量' : '体脂率 · 按 BMI 估算', value: c.fat_pct, unit: '%',
      bands: c.fat_bands, classes: ['low', 'good', 'warn', 'bad'], labels: c.fat_labels,
      min: Math.max(0, c.fat_bands[0] - 8), max: c.fat_bands[2] + 20,
      note: `脂肪 ${fmt(c.fat_mass)} kg · 去脂体重 ${fmt(c.lean_mass)} kg`,
    }),
  ];
  if (c.method === 'bia') {
    items.push(
      gauge({ name: '肌肉率', value: c.muscle_pct, unit: '%', note: `肌肉量 ${fmt(c.muscle_mass)} kg（含肌肉中的水分）` }),
      gauge({
        name: '水分率', value: c.water_pct, unit: '%', bands: c.water_range, classes: ['warn', 'good', 'low'],
        labels: ['偏低', '正常', '偏高'], min: 35, max: 75, note: '受饮水和运动影响很大，看趋势即可',
      }),
      gauge({ name: '骨量', value: c.bone_mass, unit: 'kg', digits: 2, note: '成年后基本稳定，短期变化不用在意' }),
    );
  }
  items.push(gauge({
    name: '基础代谢', value: c.bmr_katch, unit: 'kcal', digits: 0,
    note: `按去脂体重（Katch-McArdle）计算${bmrMifflin ? `；计划用的 Mifflin 公式为 ${kcal(bmrMifflin)} kcal` : ''}`,
  }));
  if (heartRate) items.push(gauge({ name: '心率', value: heartRate, unit: '次/分', digits: 0, note: '称重时测得，站立静息心率' }));
  if (target) {
    items.push(gauge({
      name: '目标体重', value: target.weight, unit: 'kg', digits: 1,
      note: `对应 BMI ${fmt(target.bmi)}（${esc(target.bmi_labels[target.bmi_level])}），距离当前 ${fmt(c.weight - target.weight)} kg`,
    }));
  }
  return items.join('');
}

// ---------- 身体成分 ----------

async function loadBody() {
  bodyData = await api('/api/body');
  renderBody();
}

function renderBody() {
  const data = bodyData;
  $('#bodyEmpty').hidden = data.ready;
  $('#bodyContent').hidden = !data.ready;
  if (!data.ready) return;
  const hasBia = !!data.latest_bia;
  const source = hasBia ? bodySource : 'trend';
  for (const b of $$('#bodySourceSeg button')) {
    b.setAttribute('aria-checked', String(b.dataset.source === source));
    b.disabled = b.dataset.source === 'bia' && !hasBia;
  }
  const c = source === 'bia' ? data.latest_bia.composition : (data.latest.trend_composition ?? data.latest.composition);
  $('#bodySub').textContent = source === 'bia'
    ? `${humanDay(data.latest_bia.measured_at)} ${data.latest_bia.measured_at.slice(11, 16)} 体重秤光脚测量 · ${fmt(c.weight, 2)} kg · 50 kHz 阻抗 ${fmt(data.latest_bia.impedance_low)} Ω`
    : `按 ${humanDay(data.latest.day)} 的趋势体重 ${fmt(c.weight, 2)} kg 估算（没有阻抗，只能算 BMI 相关指标）`;
  $('#bodyGauges').innerHTML = compositionGauges(c, {
    bmrMifflin: lastDash?.card?.burn_bmr,
    heartRate: source === 'bia' ? data.latest_bia.heart_rate : null,
    target: data.target,
  });
  renderCompBars(c, data.target);
  renderBodyTable(data);
  renderBodyCharts(data);
}

function compBar(label, c) {
  const parts = c.method === 'bia'
    ? [['脂肪', c.fat_mass, '--c-fat'], ['肌肉', c.muscle_mass, '--c-muscle'], ['骨骼', c.bone_mass, '--c-bone']]
    : [['脂肪', c.fat_mass, '--c-fat'], ['去脂体重', c.lean_mass, '--c-muscle']];
  const total = parts.reduce((s, [, v]) => s + v, 0);
  return `
    <div class="comp">
      <div class="comp-label"><span>${esc(label)}</span><b>${fmt(c.weight)} kg</b></div>
      <div class="comp-bar" role="img" aria-label="${esc(parts.map(([n, v]) => `${n} ${fmt(v)} kg`).join('，'))}">
        ${parts.map(([n, v, col]) => `<span style="flex:${v / total};background:var(${col})" title="${n} ${fmt(v)} kg"></span>`).join('')}
      </div>
      <div class="comp-legend">
        ${parts.map(([n, v, col]) => `<span><i style="background:var(${col})"></i>${n} ${fmt(v)} kg · ${fmt((v / c.weight) * 100)}%</span>`).join('')}
      </div>
    </div>`;
}

function renderCompBars(c, target) {
  $('#compBars').innerHTML = compBar(c.method === 'bia' ? '现在（体重秤测量）' : '现在（按 BMI 估算）', c)
    + compBar(`目标 ${fmt(target.weight)} kg（按 BMI 估算）`, target);
}

function renderBodyTable(data) {
  const now = data.latest.trend_composition ?? data.latest.composition;
  const t = data.target;
  const rows = [
    ['体重', now.weight, t.weight, 'kg', 1],
    ['BMI', now.bmi, t.bmi, '', 1],
    ['体脂率（估算）', now.fat_pct, t.fat_pct, '%', 1],
    ['脂肪量', now.fat_mass, t.fat_mass, 'kg', 1],
    ['去脂体重', now.lean_mass, t.lean_mass, 'kg', 1],
    ['基础代谢', now.bmr_katch, t.bmr_katch, 'kcal', 0],
  ];
  $('#bodyTable').innerHTML = `
    <thead><tr><th>指标</th><th class="num">现在（趋势体重）</th><th class="num">目标</th><th class="num">变化</th></tr></thead>
    <tbody>${rows.map(([n, a, b, u, d]) => `<tr><td>${n}</td><td class="num">${fmt(a, d)} ${u}</td><td class="num">${fmt(b, d)} ${u}</td><td class="num">${signed(b - a, d)}</td></tr>`).join('')}</tbody>`;
}

function bandAreas(bands, classes, col, min, max) {
  const fill = { good: col.goodBand, warn: col.warnBand, bad: col.badBand, low: col.lowBand };
  const edges = [min, ...bands, max];
  return classes.map((c, i) => [
    { yAxis: edges[i], itemStyle: { color: fill[c] } },
    { yAxis: edges[i + 1] },
  ]);
}

function timeAxis(col) {
  return {
    type: 'time', axisTick: { show: false }, axisLine: { lineStyle: { color: col.axis } }, splitLine: { show: false },
    axisLabel: { color: col.ink3, fontSize: 11, hideOverlap: true, formatter: (v) => { const d = new Date(v); return `${d.getMonth() + 1}/${d.getDate()}`; } },
  };
}

const pairs = (dates, values) => dates.map((d, i) => [d, values[i]]).filter(([, v]) => v != null);

function smallChartOption(col, dates, legend) {
  return {
    animation: false,
    textStyle: { color: col.ink2, fontFamily: 'system-ui, -apple-system, "PingFang SC", sans-serif' },
    grid: { left: 8, right: 16, top: legend ? 36 : 16, bottom: 8, containLabel: true },
    legend: legend ? { top: 0, left: 0, itemWidth: 16, itemHeight: 8, textStyle: { color: col.ink2, fontSize: 12 }, data: legend } : undefined,
    tooltip: {
      trigger: 'axis', confine: true, backgroundColor: col.surface, borderColor: col.grid,
      textStyle: { color: col.ink, fontSize: 12 }, axisPointer: { type: 'line', lineStyle: { color: col.axis } },
    },
    xAxis: {
      type: 'category', data: dates, boundaryGap: false, axisTick: { show: false },
      axisLine: { lineStyle: { color: col.axis } },
      axisLabel: { color: col.ink3, fontSize: 11, hideOverlap: true, formatter: (v) => `${Number(v.slice(5, 7))}/${Number(v.slice(8))}` },
    },
    yAxis: { type: 'value', scale: true, splitNumber: 4, axisLabel: { color: col.ink3, fontSize: 11 }, splitLine: { lineStyle: { color: col.grid } } },
  };
}

function statusBandColors() {
  const cs = getComputedStyle(document.documentElement);
  const mix = (name, a) => {
    const v = cs.getPropertyValue(name).trim();
    const n = parseInt(v.slice(1), 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
  };
  return { goodBand: mix('--good', 0.08), warnBand: mix('--warning', 0.10), badBand: mix('--critical', 0.07), lowBand: 'rgba(137,135,129,0.06)' };
}

function renderBodyCharts(data) {
  const col = { ...themeColors(), ...statusBandColors() };
  const s = data.series;
  bmiChart ??= echarts.init($('#bmiChart'));
  fatChart ??= echarts.init($('#fatChart'));

  const bmiVals = s.bmi.filter((v) => v != null);
  const bmiMin = Math.floor(Math.min(...bmiVals, 24) - 1);
  const bmiMax = Math.ceil(Math.max(...bmiVals) + 1);
  const bmiBase = smallChartOption(col, s.dates);
  bmiChart.setOption({
    ...bmiBase,
    xAxis: timeAxis(col),
    yAxis: { ...bmiBase.yAxis, min: bmiMin, max: bmiMax },
    tooltip: { ...bmiBase.tooltip, valueFormatter: (v) => fmt(v) },
    series: [{
      name: 'BMI', type: 'line', data: pairs(s.dates, s.bmi), smooth: true, symbol: 'none',
      lineStyle: { width: 2.5, color: col.trend }, itemStyle: { color: col.trend },
      markArea: { silent: true, data: bandAreas([18.5, 24, 28], ['warn', 'good', 'warn', 'bad'], col, 0, 60) },
      markLine: {
        silent: true, symbol: 'none', lineStyle: { color: col.axis, type: 'solid', width: 1 },
        label: { color: col.ink3, fontSize: 11, position: 'insideEndTop', formatter: (p) => (p.value === 24 ? '超重 24' : '肥胖 28') },
        data: [24, 28].filter((v) => v > bmiMin && v < bmiMax).map((v) => ({ yAxis: v })),
      },
    }],
  }, true);

  const bands = (data.latest_bia?.composition ?? data.latest.composition).fat_bands;
  const fatVals = [...s.fat_est, ...s.fat_bia].filter((v) => v != null);
  const fatMin = Math.floor(Math.min(...fatVals, bands[1]) - 2);
  const fatMax = Math.ceil(Math.max(...fatVals) + 2);
  const legend = s.fat_bia.some((v) => v != null) ? ['按 BMI 估算', '体重秤测量'] : null;
  const base = smallChartOption(col, s.dates, legend);
  fatChart.setOption({
    ...base,
    xAxis: timeAxis(col),
    yAxis: { ...base.yAxis, min: fatMin, max: fatMax, axisLabel: { ...base.yAxis.axisLabel, formatter: '{value}%' } },
    tooltip: { ...base.tooltip, valueFormatter: (v) => (v == null ? '—' : `${fmt(v)}%`) },
    series: [
      {
        name: '按 BMI 估算', type: 'line', data: pairs(s.dates, s.fat_est), smooth: true, symbol: 'none',
        lineStyle: { width: 2.5, color: col.trend }, itemStyle: { color: col.trend },
        markArea: { silent: true, data: bandAreas(bands, ['low', 'good', 'warn', 'bad'], col, 0, 80) },
      },
      {
        name: '体重秤测量', type: 'scatter', data: pairs(s.dates, s.fat_bia), symbolSize: 10,
        itemStyle: { color: col.proj, borderColor: col.surface, borderWidth: 2 },
      },
    ],
  }, true);
  bmiChart.resize();
  fatChart.resize();
}

$('#bodySourceSeg').addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (!b || b.disabled) return;
  bodySource = b.dataset.source;
  savePref('bodySource', bodySource);
  renderBody();
});

// ---------- 每日报告 ----------

async function loadReport(day) {
  const data = await api(`/api/report${day ? `?day=${day}` : ''}`);
  $('#reportEmpty').hidden = data.ready;
  $('#reportContent').hidden = !data.ready;
  if (!data.ready) return;
  reportDay = day ? data.day : null; // 没指定日期时始终跟随最新一天
  renderReport(data);
}

function deltaSpan(v, digits = 2, goodWhenDown = true) {
  if (v == null) return '—';
  const cls = Math.abs(v) < 0.005 ? '' : (v < 0) === goodWhenDown ? 'delta-good' : 'delta-bad';
  return `<span class="${cls}">${signed(v, digits)}</span>`;
}

function renderReport(r) {
  $('#reportTitle').textContent = `${Number(r.day.slice(5, 7))}月${Number(r.day.slice(8))}日 ${weekday(r.day)}`;
  $('#reportDay').innerHTML = r.days.slice().reverse()
    .map((d) => `<option value="${d}"${d === r.day ? ' selected' : ''}>${d} ${weekday(d)}</option>`).join('');
  $('#reportPrev').disabled = !r.prev_day;
  $('#reportNext').disabled = !r.next_day;
  $('#reportPrev').dataset.day = r.prev_day ?? '';
  $('#reportNext').dataset.day = r.next_day ?? '';

  let status = '';
  if (r.diff != null) {
    const st = r.diff <= -0.5 ? STATUS.ahead : r.diff <= 0 ? STATUS.on_track : r.diff <= 0.5 ? STATUS.slightly_behind : STATUS.behind;
    status = `<span class="pill ${st.cls}">${st.text}</span>`;
  }
  $('#reportKpis').innerHTML = `
    <div class="card kpi">
      <div class="eyebrow">当天体重</div>
      <div class="kpi-value">${fmt(r.weight, 2)}<small>kg</small></div>
      <div class="kpi-sub">比上次称重（${r.prev_day ? humanDay(r.prev_day) : '—'}）${deltaSpan(r.change_vs_prev)} kg</div>
    </div>
    <div class="card kpi">
      <div class="eyebrow">趋势体重</div>
      <div class="kpi-value">${fmt(r.trend, 2)}<small>kg</small></div>
      <div class="kpi-sub">近 7 天 ${deltaSpan(r.trend_change_7d)} kg</div>
    </div>
    <div class="card kpi">
      <div class="eyebrow">对比计划</div>
      <div class="kpi-value">${r.plan == null ? '—' : fmt(r.plan, 2)}<small>${r.plan == null ? '不在计划期内' : 'kg'}</small></div>
      <div class="kpi-sub">${r.diff == null ? '' : `趋势${r.diff > 0 ? '高出' : '低于'}计划 ${fmt(Math.abs(r.diff), 2)} kg `}${status}</div>
    </div>
    <div class="card kpi">
      <div class="eyebrow">当天热量</div>
      <div class="kpi-value">${kcal(r.intake)}<small>kcal 建议摄入</small></div>
      <div class="kpi-sub">预计消耗 ${kcal(r.burn)} · 缺口 ${kcal(r.deficit)} kcal</div>
    </div>`;

  const rows = r.weighins.slice().reverse();
  $('#reportWeighins').innerHTML = rows.map((w) => {
    const extras = [];
    if (w.heart_rate) extras.push(`心率 ${w.heart_rate}`);
    if (w.impedance_low) extras.push(`阻抗 ${Math.round(w.impedance_low)}${w.impedance_high ? `/${Math.round(w.impedance_high)}` : ''} Ω`);
    const counted = w.id === r.counted.id;
    if (!counted) extras.push('不计入');
    return `<li><button type="button" class="history-row${counted ? '' : ' h-ignored'}" data-id="${w.id}">
      <span class="h-date">${esc(w.measured_at.slice(11, 16))}</span>
      <span class="h-meta"><span class="h-source">${esc(SOURCE[w.source] ?? w.source)}</span>${esc(extras.join(' · '))}</span>
      <span class="h-delta"></span>
      <span class="h-weight">${fmt(w.weight_kg, 2)}<small>kg</small></span>
    </button></li>`;
  }).join('');
  reportRecords = r.weighins;

  const c = r.bia ? r.bia.composition : r.composition;
  $('#reportBodySub').textContent = r.bia
    ? `${r.bia.measured_at.slice(11, 16)} 体重秤光脚测量，50 kHz 阻抗 ${fmt(r.bia.impedance_low)} Ω`
    : '当天没有阻抗数据，按体重和 BMI 估算';
  $('#reportGauges').innerHTML = compositionGauges(c, { heartRate: r.bia?.heart_rate });
  renderReportChart(r);
}

let reportRecords = [];

function renderReportChart(r) {
  if (!lastDash?.ready) return;
  const col = themeColors();
  const s = lastDash.series;
  const i = s.dates.indexOf(r.day);
  if (i < 0) return;
  const lo = Math.max(0, i - 14);
  const hi = Math.min(s.dates.length - 1, i + 14, s.dates.indexOf(lastDash.today) >= 0 ? s.dates.indexOf(lastDash.today) : s.dates.length - 1);
  const slice = (arr) => arr.slice(lo, hi + 1);
  const dates = slice(s.dates);
  reportChart ??= echarts.init($('#reportChart'));
  const base = smallChartOption(col, dates, ['趋势体重', '每日称重', '计划曲线']);
  reportChart.setOption({
    ...base,
    tooltip: { ...base.tooltip, valueFormatter: (v) => (v == null ? '—' : `${fmt(v, 2)} kg`) },
    series: [
      {
        name: '计划曲线', type: 'line', data: slice(s.plan), symbol: 'none', smooth: true,
        lineStyle: { type: [6, 4], width: 1.5, color: col.plan }, itemStyle: { color: col.plan },
        markLine: {
          silent: true, symbol: 'none', data: [{ xAxis: r.day }],
          lineStyle: { color: col.axis, type: 'solid', width: 1 }, label: { formatter: '当天', color: col.ink3, fontSize: 11 },
        },
      },
      {
        name: '每日称重', type: 'scatter', data: slice(s.actual), symbolSize: 8,
        itemStyle: { color: col.dots, borderColor: col.surface, borderWidth: 1.5 },
      },
      {
        name: '趋势体重', type: 'line', data: slice(s.trend), symbol: 'none', smooth: true, connectNulls: true,
        lineStyle: { width: 2.5, color: col.trend }, itemStyle: { color: col.trend },
      },
    ],
  }, true);
  reportChart.resize();
}

const goReport = (day) => { location.hash = day ? `#report/${day}` : '#report'; };
$('#reportPrev').addEventListener('click', (e) => goReport(e.currentTarget.dataset.day));
$('#reportNext').addEventListener('click', (e) => goReport(e.currentTarget.dataset.day));
$('#reportDay').addEventListener('change', (e) => goReport(e.target.value));
$('#reportWeighins').addEventListener('click', (e) => {
  const row = e.target.closest('.history-row');
  const record = row && reportRecords.find((w) => w.id === Number(row.dataset.id));
  if (record) openWeigh(record);
});

// ---------- 体重选择器 ----------

const picker = {
  value: 70,
  min: 20,
  max: 300,
  time: '',        // 'YYYY-MM-DDTHH:MM'，空表示「现在」
  when: 'now',
  editing: null,   // 正在修改的记录
  syncing: false,  // 程序设置 scrollLeft 时忽略 scroll 事件
};
const ruler = $('#ruler');
const rulerTrack = $('#rulerTrack');

function buildRuler(center) {
  picker.min = Math.max(20, Math.floor(center - 25));
  picker.max = Math.min(300, Math.ceil(center + 25));
  const half = ruler.clientWidth / 2;
  const span = (picker.max - picker.min) * PX_PER_KG;
  rulerTrack.style.width = `${span + half * 2}px`;
  let html = `<div class="ruler-ticks" style="left:${half}px;width:${span + 2}px;--px-kg:${PX_PER_KG}px"></div>`;
  for (let kg = picker.min; kg <= picker.max; kg += 1) {
    html += `<span class="ruler-label" style="left:${half + (kg - picker.min) * PX_PER_KG}px">${kg}</span>`;
  }
  rulerTrack.innerHTML = html;
}

function referenceWeight() {
  return lastHistory[0]?.weight_kg ?? lastDash?.card?.trend_weight ?? 70;
}

function renderPickerValue() {
  $('#pickValue').textContent = picker.value.toFixed(2);
  ruler.setAttribute('aria-valuenow', picker.value.toFixed(2));
  ruler.setAttribute('aria-valuetext', `${picker.value.toFixed(2)} 公斤`);
  const ref = picker.editing ? picker.editing.weight_kg : lastHistory[0]?.weight_kg;
  const delta = $('#pickDelta');
  if (ref == null) { delta.textContent = ''; return; }
  const d = picker.value - ref;
  const label = picker.editing ? '原记录' : `上次（${humanDay(lastHistory[0].measured_at)}）`;
  delta.textContent = Math.abs(d) < 0.001 ? `与${label}相同` : `比${label} ${signed(d)} kg`;
}

function setPickerValue(v, { scroll = true, smooth = false } = {}) {
  const value = Math.min(300, Math.max(20, roundStep(v)));
  picker.value = +value.toFixed(2);
  if (value < picker.min + 1 || value > picker.max - 1) buildRuler(value);
  renderPickerValue();
  if (scroll) {
    picker.syncing = true;
    ruler.scrollTo({ left: (value - picker.min) * PX_PER_KG, behavior: smooth ? 'smooth' : 'instant' });
    requestAnimationFrame(() => { picker.syncing = false; });
  }
}

let snapTimer = null;
let dragging = null;
ruler.addEventListener('scroll', () => {
  if (picker.syncing) return;
  const v = picker.min + ruler.scrollLeft / PX_PER_KG;
  setPickerValue(v, { scroll: false });
  clearTimeout(snapTimer);
  snapTimer = setTimeout(() => { if (!dragging) setPickerValue(picker.value, { smooth: true }); }, 140);
}, { passive: true });

// 鼠标拖动（触屏和触控板用原生滚动）
ruler.addEventListener('pointerdown', (e) => {
  if (e.pointerType !== 'mouse') return;
  dragging = { x: e.clientX, left: ruler.scrollLeft };
  ruler.classList.add('dragging');
  ruler.setPointerCapture(e.pointerId);
});
ruler.addEventListener('pointermove', (e) => {
  if (!dragging) return;
  ruler.scrollLeft = dragging.left - (e.clientX - dragging.x);
});
const endDrag = () => {
  if (!dragging) return;
  dragging = null;
  ruler.classList.remove('dragging');
  setPickerValue(picker.value, { smooth: true });
};
ruler.addEventListener('pointerup', endDrag);
ruler.addEventListener('pointercancel', endDrag);

// 鼠标滚轮上下滚动时转成左右
ruler.addEventListener('wheel', (e) => {
  if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
  e.preventDefault();
  ruler.scrollLeft += e.deltaY;
}, { passive: false });

ruler.addEventListener('keydown', (e) => {
  const steps = { ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1, PageDown: -20, PageUp: 20 };
  if (!(e.key in steps)) return;
  e.preventDefault();
  setPickerValue(picker.value + steps[e.key] * STEP * (e.shiftKey ? 10 : 1));
});

// − / + 按钮：点一下 0.05，按住连续调整
function holdRepeat(btn, dir) {
  let timer = null;
  const stop = () => { clearTimeout(timer); timer = null; };
  const tick = (delay) => {
    setPickerValue(picker.value + dir * STEP);
    timer = setTimeout(() => tick(Math.max(40, delay * 0.85)), delay);
  };
  btn.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    btn.setPointerCapture(e.pointerId);
    tick(380);
  });
  for (const ev of ['pointerup', 'pointercancel', 'lostpointercapture']) btn.addEventListener(ev, stop);
  btn.addEventListener('click', (e) => { if (e.detail === 0) setPickerValue(picker.value + dir * STEP); });
}
holdRepeat($('#pickMinus'), -1);
holdRepeat($('#pickPlus'), 1);

function setWhen(when, time) {
  picker.when = when;
  const now = new Date();
  if (when === 'now') picker.time = '';
  else if (when === 'morning') picker.time = `${todayIso()}T${MORNING}`;
  else if (when === 'yesterday') picker.time = `${isoAddDays(todayIso(), -1)}T${MORNING}`;
  else picker.time = time ?? (picker.time || localIso(now));
  for (const c of $$('#timeChips .chip')) c.setAttribute('aria-pressed', String(c.dataset.when === when));
  const input = $('#pickTime');
  input.hidden = when !== 'custom';
  input.max = localIso(now);
  if (when === 'custom') input.value = picker.time;
  const shown = picker.time || localIso(now);
  $('#timeLabel').textContent = when === 'custom' ? '' : `${humanDay(shown)} ${weekday(shown)} ${shown.slice(11, 16)}`;
}

$('#timeChips').addEventListener('click', (e) => {
  const chip = e.target.closest('.chip');
  if (chip) setWhen(chip.dataset.when);
});
$('#pickTime').addEventListener('change', (e) => { if (e.target.value) picker.time = e.target.value; });

function openWeigh(record = null) {
  picker.editing = record;
  $('#weighTitle').textContent = record ? '修改记录' : '记录体重';
  $('#deleteWeigh').hidden = !record;
  $('#saveWeigh').textContent = record ? '保存修改' : '保存';
  setMsg($('#weighMsg'), '');
  const dlg = $('#weighDialog');
  dlg.showModal();
  const start = record ? record.weight_kg : referenceWeight();
  buildRuler(start);
  setPickerValue(start);
  if (record) setWhen('custom', record.measured_at.slice(0, 16));
  else setWhen('now');
  ruler.focus({ preventScroll: true });
}

$('#weighForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const msg = $('#weighMsg');
  const record = picker.editing;
  const body = { weight_kg: picker.value };
  if (picker.time) {
    // 修改时间没动过就保留原来的秒和时区
    body.measured_at = record && picker.time === record.measured_at.slice(0, 16) ? record.measured_at : picker.time;
  }
  if (picker.time && picker.time > localIso(new Date())) {
    setMsg(msg, '称重时间不能晚于现在', 'error');
    return;
  }
  const btn = $('#saveWeigh');
  btn.disabled = true;
  try {
    if (record) {
      await api(`/api/weighins/${record.id}`, { method: 'PATCH', body: JSON.stringify(body) });
    } else {
      await api('/api/weighins', { method: 'POST', body: JSON.stringify(body) });
    }
    $('#weighDialog').close();
    toast(record ? '已修改' : `已记录 ${picker.value.toFixed(2)} kg`);
    await refresh();
  } catch (err) {
    setMsg(msg, `保存失败：${err.message}`, 'error');
  } finally {
    btn.disabled = false;
  }
});

$('#deleteWeigh').addEventListener('click', async () => {
  const record = picker.editing;
  if (!record) return;
  const label = `${record.measured_at.slice(0, 16).replace('T', ' ')} 的 ${fmt(record.weight_kg, 2)} kg`;
  if (!confirm(`删除 ${label}？删除后无法恢复。`)) return;
  try {
    await api(`/api/weighins/${record.id}`, { method: 'DELETE' });
    $('#weighDialog').close();
    toast('已删除');
    await refresh();
  } catch (err) {
    setMsg($('#weighMsg'), `删除失败：${err.message}`, 'error');
  }
});

$('#history').addEventListener('click', (e) => {
  const row = e.target.closest('.history-row');
  if (!row) return;
  const record = lastHistory.find((r) => r.id === Number(row.dataset.id));
  if (record) openWeigh(record);
});

$('#moreHistory').addEventListener('click', async () => {
  historyLimit = historyLimit === HISTORY_SHORT ? HISTORY_LONG : HISTORY_SHORT;
  lastHistory = await api(`/api/weighins?limit=${historyLimit}`);
  renderHistory(lastHistory);
});

// ---------- 计划设置 ----------

function openSettings() {
  setMsg($('#profileMsg'), '');
  $('#settingsDialog').showModal();
}

api('/api/session').then((s) => { $('#logoutBtn').hidden = !s.auth_enabled; }).catch(() => {});
$('#logoutBtn').addEventListener('click', async () => {
  await fetch('/api/logout', { method: 'POST' });
  location.replace('/login');
});

$('[data-radio="sex"]').addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (b) setRadio('sex', b.dataset.value);
});

$('#profileForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target;
  const msg = $('#profileMsg');
  const body = {
    sex: $('[data-radio="sex"]').dataset.value,
    age: Number(f.age.value),
    height_cm: Number(f.height_cm.value),
    activity: f.activity.value,
    target_weight: Number(f.target_weight.value),
    plan_start: f.plan_start.value,
    target_date: f.target_date.value,
    exercise_kcal: Number(f.exercise_kcal.value || 0),
    start_weight: f.start_weight.value ? Number(f.start_weight.value) : null,
  };
  try {
    await api('/api/profile', { method: 'PUT', body: JSON.stringify(body) });
    await refresh();
    setMsg(msg, '计划已保存，下方是新的计划摘要');
    toast('计划已保存');
  } catch (err) {
    setMsg(msg, `保存失败：${err.message}`, 'error');
  }
});

$('#importBtn').addEventListener('click', async () => {
  const msg = $('#importMsg');
  const csv = $('#importText').value;
  if (!csv.trim()) return;
  try {
    const res = await api('/api/weighins/import', { method: 'POST', body: JSON.stringify({ csv }) });
    const skipped = res.skipped_lines.length ? `，跳过第 ${res.skipped_lines.join('、')} 行` : '';
    setMsg(msg, `导入 ${res.imported} 条${skipped}`, res.skipped_lines.length ? 'error' : 'ok');
    if (res.imported) $('#importText').value = '';
    await refresh();
  } catch (err) {
    setMsg(msg, `导入失败：${err.message}`, 'error');
  }
});

// ---------- 全局 ----------

$('#openWeigh').addEventListener('click', () => openWeigh());
$('#openSettings').addEventListener('click', openSettings);

for (const dlg of $$('dialog')) {
  dlg.addEventListener('click', (e) => {
    if (e.target === dlg || e.target.closest('[data-close]')) dlg.close();
  });
}

$('#rangeSeg').addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  range = b.dataset.range;
  savePref('range', range);
  renderRange();
  renderCharts(lastDash);
});

let resizeTimer = null;
window.addEventListener('resize', () => {
  for (const c of [weightChart, deltaChart, calChart, bmiChart, fatChart, reportChart]) c?.resize();
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => renderCharts(lastDash), 150); // 跨过窄屏断点时重排图例
});
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  renderCharts(lastDash);
  if (view === 'body' && bodyData?.ready) renderBodyCharts(bodyData);
  if (view === 'report') loadReport(reportDay).catch(() => {});
});

// ---------- 训练 ----------

// 器械示意图（64×64）：m 器械、f 人体、h 头、a 动作方向
const fig = (head, parts) => `<svg viewBox="0 0 64 64" fill="none" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${
  Object.entries(parts).map(([cls, d]) => `<path class="${cls}" d="${d}"/>`).join('')
}<circle class="h" cx="${head[0]}" cy="${head[1]}" r="4.5"/></svg>`;
const ARROW_R = (x, y) => `M${x} ${y}h10m0 0l-3.5-3.5m3.5 3.5l-3.5 3.5`;
const ARROW_L = (x, y) => `M${x} ${y}h-10m0 0l3.5-3.5m-3.5 3.5l3.5 3.5`;
const ARROW_U = (x, y) => `M${x} ${y}v-10m0 0l-3.5 3.5m3.5-3.5l3.5 3.5`;
const ARROW_D = (x, y) => `M${x} ${y}v10m0 0l-3.5-3.5m3.5 3.5l3.5-3.5`;
const EQUIP = {
  leg_press: fig([22, 19], { m: 'M8 55H58M56 55L54 30M12 46L20 24M12 46H28M46 17L55 31', f: 'M20 26L16 42L33 32L50 23M19 31L25 41', a: 'M36 44l8-5m0 0h-4.5m4.5 0l-1.5 4' }),
  leg_ext: fig([16, 11], { m: 'M8 55H42M24 41V55M13 41H31M13 41L11 15M45 36a3 3 0 1 0 0.1 0', f: 'M16 17L17 38H31L47 31M16 23L23 35', a: ARROW_U(54, 42) }),
  leg_curl: fig([16, 11], { m: 'M8 55H44M24 41V55M13 41H31M13 41L11 15M20 34H38M33 50a3 3 0 1 0 0.1 0', f: 'M16 17L17 38H34L31 50M16 23L23 33', a: ARROW_D(44, 34) }),
  hip_abd: fig([32, 11], { m: 'M22 42H42M9 38V52M55 38V52M32 44V56M24 56H40', f: 'M32 17V40M32 40L17 46V55M32 40L47 46V55M32 22L25 33M32 22L39 33', a: `${ARROW_L(14, 28)}${ARROW_R(50, 28)}` }),
  calf_raise: fig([32, 9], { m: 'M18 52H46V57H18Z', f: 'M32 15V34M32 34L30 47L28 51M32 34L34 47L36 51M32 19L26 31M32 19L38 31', a: ARROW_U(50, 40) }),
  mat: fig([13, 35], { m: 'M4 55H60', f: 'M18 38L52 46L54 53M20 40V52H30' }),
  chest_press: fig([17, 10], { m: 'M8 56H40M14 44L12 14M14 44H30M22 44V56M44 18V30M44 24L52 12M52 12V56', f: 'M17 17L18 40H32V54M18 24H44', a: ARROW_R(26, 33) }),
  shoulder_press: fig([16, 12], { m: 'M8 56H40M14 44L12 14M14 44H30M22 44V56M21 5H35M44 5V56M35 5H44', f: 'M17 19L18 40H32V54M18 22L26 18L28 6', a: ARROW_U(37, 26) }),
  pec_fly: fig([32, 12], { m: 'M24 44H40M32 44V56M24 56H40M7 18V34M57 18V34', f: 'M32 18V42M32 22H20L10 27M32 22H44L54 27M32 42L26 52M32 42L38 52', a: `${ARROW_R(12, 36)}${ARROW_L(52, 36)}` }),
  cable: fig([28, 9], { m: 'M52 4V58M44 58H60M46 9L37 31', f: 'M28 15V37M28 37L24 56M28 37L32 56M28 20L30 31H37', a: ARROW_D(42, 36) }),
  lat_pulldown: fig([32, 21], { m: 'M8 4H56M32 4V12M14 12H50M22 42H42M24 48H40M32 48V58', f: 'M32 27V44M32 30L20 22L18 12M32 30L44 22L46 12M32 44L25 56M32 44L39 56', a: ARROW_D(8, 20) }),
  row: fig([16, 16], { m: 'M4 57H60M10 45H22M16 45V57M46 38L48 52M58 30V57M58 32L22 36', f: 'M16 22V43L30 34L46 44M16 27L8 34L22 36', a: ARROW_L(38, 26) }),
  back_ext: fig([56, 14], { m: 'M14 54L42 28M10 55H22M40 26L46 32', f: 'M18 50L40 31L52 20M46 25L51 28', a: 'M58 34q4-6 2-12m0 0l-3.6 1.6m3.6-1.6l1.4 3.8' }),
  treadmill: fig([30, 9], { m: 'M6 54L54 44M48 45L52 14M48 14H60M10 54V58M50 46V58', f: 'M30 15L29 33M29 33L24 48M29 33L37 46M30 19L25 28M30 19L36 27' }),
};
const TPL_SHORT = { A: '下肢', B: '推', C: '拉', R: '恢复' };
const TPL_LETTER = { A: 'A', B: 'B', C: 'C', R: '恢' };
const WEIGHT_STEP = 2.5;

let trainPlan = null;
let trainState = null;
let trainSaveTimer = null;

const isFutureDay = (day) => day > todayIso();

async function loadTraining(day) {
  if (trainSaveTimer) await flushTraining();
  trainPlan = await api(`/api/training${day ? `?day=${day}` : ''}`);
  renderTraining();
}

function checkRow(item, disabled) {
  const on = trainState.checked.includes(item.id);
  return `<li><button type="button" class="check-row" data-check="${esc(item.id)}" aria-pressed="${on}" ${disabled ? 'disabled' : ''}>
    <span class="check-box"><svg><use href="#i-check"/></svg></span>
    <span><span class="check-title">${esc(item.title)}</span><span class="check-detail">${esc(item.detail)}</span></span>
  </button></li>`;
}

function blockHead(n, title, minutes, extra = '') {
  return `<div class="block-head"><span class="step">${n}</span><h2>${title}</h2><span class="muted">${extra ? `${extra} · ` : ''}约 ${Math.round(minutes)} 分钟</span></div>`;
}

function weightLabel(ex) {
  const w = trainState.weights[ex.id];
  return w == null ? '<span class="muted">—</span>' : `${fmt(w, w % 1 ? 1 : 0)}<small>kg</small>`;
}

function exerciseCard(ex, disabled) {
  const done = ex.sets.every((s) => trainState.checked.includes(s.id));
  const unit = ex.hold ? '秒' : '次';
  const rest = ex.rest_s >= 60 && ex.rest_s % 60 === 0 ? `${ex.rest_s / 60} 分钟` : `${ex.rest_s} 秒`;
  const prog = ex.progression;
  return `<article class="card ex${done ? ' done' : ''}" data-ex="${ex.id}">
    <div class="ex-fig">${EQUIP[ex.equipment] || EQUIP.mat}</div>
    <div class="ex-body">
      <div class="ex-head"><h3>${esc(ex.name)}</h3>${ex.main ? '<span class="tag main">主项</span>' : ''}</div>
      <div class="ex-meta">${ex.muscles.map(esc).join(' · ')}｜${ex.sets.length} 组 · 组间休息 ${rest} · 做完一组点一下</div>
      <div class="sets" role="group" aria-label="${esc(ex.name)} 每组${unit}数，做完一组点一下">
        ${ex.sets.map((s, i) => `<button type="button" class="set-chip" data-check="${s.id}" aria-pressed="${trainState.checked.includes(s.id)}"
            aria-label="第 ${i + 1} 组 ${s.reps} ${unit}" ${disabled ? 'disabled' : ''}><b>${s.reps}</b><small>${unit}</small></button>`).join('')}
      </div>
      ${ex.weighted ? `<div class="ex-weight">
        <div class="stepper">
          <button type="button" data-wstep="${ex.id}" data-delta="-1" aria-label="减轻 ${WEIGHT_STEP} kg"><svg><use href="#i-minus"/></svg></button>
          <output data-wout="${ex.id}">${weightLabel(ex)}</output>
          <button type="button" data-wstep="${ex.id}" data-delta="1" aria-label="加重 ${WEIGHT_STEP} kg"><svg><use href="#i-plus"/></svg></button>
        </div>
        <span class="ex-hint">${esc(prog.hint)}</span>
      </div>` : `<p class="ex-hint" style="margin-top:10px">${esc(prog.hint)}</p>`}
      <details class="ex-more">
        <summary>动作要点</summary>
        <p>${esc(ex.cue)}</p>
        <p><b>器械被占用：</b>${esc(ex.alt)}</p>
      </details>
    </div>
  </article>`;
}

function inclineChart(segments) {
  const W = 560; const H = 190; const L = 34; const R = 6; const T = 22; const B = 24;
  const total = segments.reduce((a, s) => a + s.minutes, 0);
  const sx = (W - L - R) / total;
  const y = (v) => T + (H - T - B) * (1 - v / 16);
  let x = L;
  const rects = segments.map((s) => {
    const w = s.minutes * sx;
    const r = `<g><title>${esc(s.label)}：${s.minutes} 分钟，${s.speed} km/h，坡度 ${s.incline}%</title>
      <rect class="seg ${s.kind}" x="${x + 1}" y="${y(s.incline)}" width="${Math.max(w - 2, 1)}" height="${y(0) - y(s.incline)}" rx="3"/>
      ${w >= 30 ? `<text class="lbl" x="${x + w / 2}" y="${y(s.incline) - 6}" text-anchor="middle">${s.incline}%</text>` : ''}
      ${w >= 46 && y(0) - y(s.incline) > 26 ? `<text class="lbl spd" x="${x + w / 2}" y="${y(0) - 8}" text-anchor="middle" style="fill:#fff">${s.speed}</text>` : ''}
    </g>`;
    x += w;
    return r;
  }).join('');
  const grid = [5, 10, 15].map((v) => `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text class="tick" x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${v}%</text>`).join('');
  const step = total > 40 ? 10 : 5;
  const ticks = [];
  for (let m = 0; m <= total - step / 2; m += step) ticks.push(`<text class="tick" x="${L + m * sx}" y="${H - 6}" text-anchor="middle">${m}</text>`);
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="跑步机坡度变化，共 ${total} 分钟">${grid}${rects}
    <line class="axis" x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}"/>${ticks.join('')}
    <text class="tick" x="${W - R}" y="${H - 6}" text-anchor="end">分钟</text></svg>`;
}

function renderTrainProgress() {
  const p = trainPlan;
  const done = trainState.checked.length;
  const total = p.progress.total;
  const c = 2 * Math.PI * 26;
  $('#trainProgress').innerHTML = `
    <svg class="ring" viewBox="0 0 64 64" aria-hidden="true"><circle class="ring-bg" cx="32" cy="32" r="26"/>
      <circle class="ring-fg" cx="32" cy="32" r="26" stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - done / total)}"/></svg>
    <span class="ring-num">${Math.round((done / total) * 100)}%</span>
    <span class="ring-text"><b>${done} / ${total}</b>已完成</span>`;
  const all = done === total;
  $('#trainReason').innerHTML = all
    ? `<span class="train-done"><svg><use href="#i-check"/></svg>今天的训练全部完成，记得补充蛋白质和水分。</span>`
    : esc(p.reason);
  for (const card of $$('.ex[data-ex]')) {
    const ex = p.blocks.strength.exercises.find((e) => e.id === card.dataset.ex);
    card.classList.toggle('done', ex.sets.every((s) => trainState.checked.includes(s.id)));
  }
}

function renderWeek(recent, day) {
  $('#trainWeek').innerHTML = recent.map((d) => {
    const pct = d.total ? Math.min(100, (d.done / d.total) * 100) : 0;
    const label = d.template ? `${TPL_SHORT[d.template]}，完成 ${d.done}/${d.total}` : '没有训练';
    return `<li class="${d.day === day ? 'current' : ''}" ${d.template ? `data-tpl="${d.template}"` : ''}>
      <a href="#training/${d.day}" aria-label="${Number(d.day.slice(5, 7))}月${Number(d.day.slice(8))}日 ${label}">
        <span class="wd">${weekday(d.day).slice(1)}</span>
        <span class="cell${d.template ? ' on' : ''}">${d.template ? TPL_LETTER[d.template] : Number(d.day.slice(8))}</span>
        <span class="done-bar"><span style="width:${pct}%"></span></span>
      </a></li>`;
  }).join('');
}

function trainingTips(p) {
  const c = p.blocks.cardio;
  const tips = [
    ['先力量、后跑步机', '力量训练放在体力最好的时候，动作更稳、重量更足；跑步机放在后面继续消耗。'],
    ['次数少一点，重量重一点', '你现在每个器械 20/18/16/14/12 递减做 4–5 组，重量偏轻，更像耐力训练。减脂期更需要有一定重量的刺激来保住肌肉：主项 15/12/10/10、每组加一点重量，每组做到最后 1–2 个吃力为止。周末时间多，主项加第 5 组。'],
    ['固定轮换，不再随便找器械', '按 A 下肢 → B 推 → C 拉 轮换，练了才往后排，所以每天去还是隔天去都不会连着练同一个部位。连续练满 3 天会安排一次恢复训练。器械被占用时，看动作要点里的替代动作。'],
    ['爬坡保留，但要有起伏', '你现在 15% 坡度、4.5 km/h 连续走 40–50 分钟，强度不低，但长时间高坡对小腿、跟腱和下背压力很大。推/拉日保留 15%，每 4 分钟降到 8% 快走 2 分钟；练腿日腿已经累了，降到 10% 匀速走。'],
    c.walk_only
      ? ['暂时不跑步', `BMI ${fmt(p.bmi)} ≥ 30，跑步时膝盖和脚踝承受的冲击是体重的 2–3 倍。先以爬坡走为主，体重降到 ${fmt(c.walk_only_until)} kg 以下再加入慢跑间歇，页面会自动调整。`]
      : ['可以加入慢跑', '体重已经降到 BMI 30 以下，推/拉日改成快走爬坡和慢跑交替。'],
    ['不要扶扶手', '扶着扶手走会少 20–30% 的消耗，也容易含胸弓背。坡度高到必须扶时，宁可把坡度降下来。'],
    ['消耗估算', `今天预计消耗约 ${kcal(p.kcal.total)} kcal（跑步机约 ${kcal(p.kcal.cardio)}，力量约 ${kcal(p.kcal.strength)}），按 ACSM 公式粗算，偏上限。计划里的活动水平已经包含了规律运动，不要再把这部分填进「每天额外运动消耗」，否则会重复计算。`],
  ];
  $('#trainTips').innerHTML = tips.map(([t, d]) => `<li><span><b>${esc(t)}</b><span class="check-detail">${esc(d)}</span></span></li>`).join('');
}

function renderTraining() {
  const p = trainPlan;
  $('#trainingEmpty').hidden = p.ready;
  $('#trainingContent').hidden = !p.ready;
  if (!p.ready) return;
  trainState = structuredClone(p.state);
  const root = $('#trainingContent');
  root.dataset.tpl = p.template;
  const future = isFutureDay(p.day);
  const today = p.day === todayIso();

  $('#trainDate').textContent = `${Number(p.day.slice(5, 7))}月${Number(p.day.slice(8))}日 ${weekday(p.day)}${today ? ' · 今天' : ''}`;
  $('#trainToday').hidden = today;
  $('#trainPreview').hidden = !future;
  const badge = $('#trainBadge');
  badge.dataset.tpl = p.template;
  badge.textContent = TPL_LETTER[p.template];
  $('#trainTitle').textContent = p.title;
  $('#trainFocus').textContent = `${p.focus} · ${p.budget} 分钟${p.weekend ? '（周末）' : ''}`;

  const b = p.blocks;
  const parts = [
    ['warm', '热身', b.warmup.minutes], ['strength', '力量', b.strength.minutes],
    ['cardio', '跑步机', b.cardio.minutes], ['cool', '拉伸', b.cooldown.minutes],
  ];
  $('#trainTimeline').innerHTML = `
    <div class="timeline-bar" role="img" aria-label="${parts.map(([, n, m]) => `${n} ${m} 分钟`).join('，')}">
      ${parts.map(([k, , m]) => `<span class="k-${k}" style="flex:${m}"></span>`).join('')}
    </div>
    <div class="timeline-legend">
      ${parts.map(([k, n, m]) => `<span><i class="k-${k}" style="background:${k === 'strength' ? 'var(--tpl)' : k === 'cardio' ? 'color-mix(in srgb, var(--tpl) 45%, var(--surface))' : 'var(--axis)'}"></i>${n} <b>${Math.round(m)}</b> 分钟</span>`).join('')}
      <span>预计消耗 <b>${kcal(p.kcal.total)}</b> kcal</span>
    </div>`;

  $('#trainTemplateSeg').innerHTML = p.templates.map((t) => `<button type="button" role="radio" data-template="${t.key}"
      aria-checked="${t.key === p.template}" ${t.key === p.auto_template ? 'data-auto' : ''}
      title="${esc(t.title)}${t.key === p.auto_template ? '（按轮换应该练这个）' : ''}">${t.key === 'R' ? '' : `${t.key} `}${TPL_SHORT[t.key]}</button>`).join('');
  $('#trainBudgetSeg').innerHTML = p.budget_options.map((m) => `<button type="button" role="radio" data-budget="${m}"
      aria-checked="${m === p.budget}">${m} 分钟</button>`).join('');

  renderWeek(p.recent, p.day);

  $('#blockWarmup').innerHTML = `${blockHead(1, '热身', b.warmup.minutes)}
    <div class="card"><ul class="checklist">${b.warmup.items.map((i) => checkRow(i, future)).join('')}</ul></div>`;
  $('#blockStrength').innerHTML = `${blockHead(2, '力量训练', b.strength.minutes, `${b.strength.exercises.length} 个动作`)}
    <div class="exercises">${b.strength.exercises.map((e) => exerciseCard(e, future)).join('')}</div>`;
  $('#blockCardio').innerHTML = `${blockHead(3, '跑步机', b.cardio.minutes)}
    <div class="card"><div class="cardio-grid">
      <div>
        <div class="incline-chart">${inclineChart(b.cardio.segments)}</div>
        <div class="hr-zone"><svg><use href="#i-heart"/></svg><span>心率保持在 <b>${p.heart_rate.zone2[0]}–${p.heart_rate.zone2[1]}</b> 次/分，能说完整的句子但唱不了歌；爬坡段可以到 ${p.heart_rate.hard[1]}。</span></div>
      </div>
      <div><ul class="checklist">${b.cardio.items.map((i) => checkRow(i, future)).join('')}</ul></div>
    </div>
    <p class="cardio-note">${esc(b.cardio.note)}</p></div>`;
  $('#blockCooldown').innerHTML = `${blockHead(4, '拉伸', b.cooldown.minutes)}
    <div class="card"><ul class="checklist">${b.cooldown.items.map((i) => checkRow(i, future)).join('')}</ul></div>`;

  trainingTips(p);
  renderTrainProgress();
}

async function flushTraining() {
  clearTimeout(trainSaveTimer);
  trainSaveTimer = null;
  const saved = await api(`/api/training/${trainPlan.day}`, { method: 'PUT', body: JSON.stringify(trainState) });
  trainPlan.recent = saved.recent;
  renderWeek(saved.recent, saved.day);
  return saved;
}

function saveTrainingSoon() {
  clearTimeout(trainSaveTimer);
  trainSaveTimer = setTimeout(() => flushTraining().catch((err) => toast(`保存失败：${err.message}`)), 500);
}

$('#trainingContent').addEventListener('click', async (e) => {
  const check = e.target.closest('[data-check]');
  if (check && !check.disabled) {
    const id = check.dataset.check;
    const on = !trainState.checked.includes(id);
    trainState.checked = on ? [...trainState.checked, id] : trainState.checked.filter((c) => c !== id);
    check.setAttribute('aria-pressed', on);
    renderTrainProgress();
    saveTrainingSoon();
    return;
  }
  const step = e.target.closest('[data-wstep]');
  if (step) {
    const id = step.dataset.wstep;
    const ex = trainPlan.blocks.strength.exercises.find((x) => x.id === id);
    const cur = trainState.weights[id];
    const base = cur ?? ex.progression.suggest;
    // 第一次点：有建议重量就直接用建议值，没有就从 20 kg 起
    const next = cur == null ? (base ?? 20) : Math.max(0, cur + Number(step.dataset.delta) * WEIGHT_STEP);
    trainState.weights[id] = next;
    $(`[data-wout="${id}"]`).innerHTML = weightLabel(ex);
    saveTrainingSoon();
    return;
  }
  const tpl = e.target.closest('[data-template]');
  const budget = e.target.closest('[data-budget]');
  if (tpl || budget) {
    if (tpl) trainState.template = tpl.dataset.template === trainPlan.auto_template ? null : tpl.dataset.template;
    if (budget) trainState.budget = Number(budget.dataset.budget) === trainPlan.default_budget ? null : Number(budget.dataset.budget);
    try {
      trainPlan = await flushTraining();
      renderTraining();
    } catch (err) {
      toast(`保存失败：${err.message}`);
    }
  }
});
$('#trainPrev').addEventListener('click', () => { location.hash = `#training/${isoAddDays(trainPlan.day, -1)}`; });
$('#trainNext').addEventListener('click', () => { location.hash = `#training/${isoAddDays(trainPlan.day, 1)}`; });
$('#trainToday').addEventListener('click', () => { location.hash = '#training'; });
window.addEventListener('pagehide', () => {
  // 离开页面前把还没保存的打勾发出去
  if (trainSaveTimer) {
    clearTimeout(trainSaveTimer);
    fetch(`/api/training/${trainPlan.day}`, {
      method: 'PUT', keepalive: true, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(trainState),
    });
  }
});

// 体重秤自动上报后不用手动刷新
const quietRefresh = () => {
  if (document.visibilityState === 'visible' && !$('dialog[open]')) refresh().catch(() => {});
};
setInterval(quietRefresh, REFRESH_MS);
document.addEventListener('visibilitychange', quietRefresh);

renderRange();
refresh().then(showView).catch((err) => {
  $('#alerts').innerHTML = `<div class="alert danger"><svg><use href="#i-alert"/></svg><div>加载失败：${esc(err.message)}</div></div>`;
});
