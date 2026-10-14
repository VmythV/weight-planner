'use strict';

const $ = (sel) => document.querySelector(sel);

const STATUS = {
  ahead: { text: '领先计划', cls: 'good' },
  on_track: { text: '按计划进行', cls: 'good' },
  slightly_behind: { text: '略微落后', cls: 'warn' },
  behind: { text: '落后计划', cls: 'bad' },
  reached: { text: '已达成目标', cls: 'good' },
  not_started: { text: '计划未开始', cls: 'muted' },
  not_applicable: { text: '维持/增重', cls: 'muted' },
};
const SOURCE = { manual: '手动', import: '导入', esp32: '体重秤' };
const KCAL_PER_KG = 7700;
const HISTORY_SHORT = 10;
const HISTORY_LONG = 200;

let weightChart = null;
let calChart = null;
let lastDash = null;
let historyLimit = HISTORY_SHORT;

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));
const fmt = (v, digits = 1) => (v == null ? '—' : Number(v).toFixed(digits));
const kcal = (v) => (v == null ? '—' : Math.round(v).toLocaleString('zh-CN'));

function isoAddDays(iso, days) {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

function nowLocalInput() {
  const d = new Date();
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 16);
}

function todayIso() {
  return nowLocalInput().slice(0, 10);
}

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
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
  el.className = `msg ${kind}`;
}

// ---------- 渲染 ----------

function renderWarnings(dash) {
  const list = dash.ready ? dash.warnings : [];
  $('#warnings').innerHTML = list
    .map((w) => `<div class="alert ${esc(w.level)}">${esc(w.text)}</div>`)
    .join('');
}

function renderEmpty(dash) {
  const box = $('#emptyState');
  if (dash.ready) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.textContent = dash.reason === 'no_profile'
    ? '先在下方「计划设置」里填写身高、目标体重和日期。'
    : '先记录一次体重，就能生成计划曲线。';
  if (dash.reason === 'no_profile') $('#settingsBox').open = true;
}

function renderCard(dash) {
  const c = dash.card;
  const plan = dash.plan;
  const st = STATUS[c.status] ?? STATUS.not_applicable;
  let diffText = '';
  if (c.diff != null) {
    diffText = c.diff > 0 ? `比计划多 ${fmt(c.diff, 2)} kg` : `比计划少 ${fmt(-c.diff, 2)} kg`;
  }
  let outlook = '';
  if (c.reached) {
    outlook = '已经达到目标，接下来按维持热量巩固。';
  } else if (c.ended) {
    outlook = '目标日已过，可以在「计划设置」里制定新计划。';
  } else if (c.projected_reach_date) {
    outlook = `按建议摄入，预计 ${c.projected_reach_date} 达到目标；目标日预计 ${fmt(c.projected_weight_at_target)} kg。`;
  }
  const exercise = c.burn_exercise ? ` + 运动 ${kcal(c.burn_exercise)}` : '';
  const steps = c.exercise_steps
    ? `<div class="sub">运动部分约等于快走 ${c.exercise_steps.toLocaleString('zh-CN')} 步</div>`
    : '';
  const latest = c.latest_weight == null ? '' : ` · 最近称重 ${fmt(c.latest_weight, 2)} kg（${esc(c.latest_date.slice(5))}）`;

  $('#card').innerHTML = `
    <div class="tile hero">
      <div class="label">今天可以摄入</div>
      <div class="big">${kcal(c.intake)}<small> kcal</small></div>
      <div class="sub">蛋白质 ${c.protein_g} g · 脂肪 ${c.fat_g} g · 碳水 ${c.carbs_g} g</div>
    </div>
    <div class="tile">
      <div class="label">今天预计消耗</div>
      <div class="big">${kcal(c.burn_total)}<small> kcal</small></div>
      <div class="sub">基础代谢 ${kcal(c.burn_bmr)} + 日常活动 ${kcal(c.burn_activity)}${exercise}</div>
      ${steps}
    </div>
    <div class="tile">
      <div class="label">热量缺口</div>
      <div class="big">${kcal(c.deficit)}<small> kcal</small></div>
      <div class="sub">约等于每周减 ${fmt((c.deficit * 7) / KCAL_PER_KG, 2)} kg</div>
    </div>
    <div class="tile">
      <div class="label">趋势体重</div>
      <div class="big">${fmt(c.trend_weight)}<small> kg</small></div>
      <div class="sub">今日计划 ${fmt(c.plan_weight_today)} kg <span class="badge ${st.cls}">${st.text}</span></div>
      <div class="sub">${diffText}${latest}</div>
    </div>
    <div class="tile wide">
      <div class="label">进度</div>
      <div class="progress"><span style="width:${c.progress_pct ?? 0}%"></span></div>
      <div class="sub">已减 ${fmt(c.lost_kg)} kg · 还差 ${fmt(c.remaining_kg)} kg · 剩余 ${c.days_left} 天 · 目标 ${fmt(plan.target_weight)} kg（${esc(plan.target_date)}）</div>
      <div class="sub">${esc(outlook)}</div>
    </div>`;
}

function themeColors() {
  const cs = getComputedStyle(document.documentElement);
  const v = (name) => cs.getPropertyValue(name).trim();
  return {
    text: v('--text'), muted: v('--muted'), grid: v('--border'), panel: v('--panel'),
    plan: v('--plan'), band: v('--band'), trend: v('--accent'), actual: v('--actual'),
    proj: v('--proj'), burn: v('--burn'), intake: v('--intake'), deficit: v('--deficit'),
  };
}

function tooltipHtml(dash, i) {
  const s = dash.series;
  const rows = [
    ['计划体重', s.plan[i], 'kg'],
    ['当天称重', s.actual[i], 'kg'],
    ['趋势体重', s.trend[i], 'kg'],
    ['预测体重', s.projection[i], 'kg'],
    ['建议摄入', s.intake[i], 'kcal'],
    ['预计消耗', s.burn[i], 'kcal'],
    ['热量缺口', s.deficit[i], 'kcal'],
  ].filter((r) => r[1] != null);
  const lines = rows.map(([k, v, u]) => `${k}：${u === 'kg' ? fmt(v, 2) : kcal(v)} ${u}`);
  return [`<b>${esc(s.dates[i])}</b>`, ...lines].join('<br>');
}

function baseOption(dash, col, legendWraps) {
  const dates = dash.series.dates;
  const start = [dates[0], isoAddDays(dash.plan.plan_start, -14)].sort()[1];
  const zoom = { startValue: start, endValue: dates[dates.length - 1] };
  const twoRows = legendWraps && $('#weightChart').clientWidth < 520; // 窄屏时长图例会折成两行
  return {
    animation: false,
    textStyle: { color: col.text },
    grid: { left: 44, right: 16, top: twoRows ? 72 : 48, bottom: 64 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: col.panel,
      borderColor: col.grid,
      textStyle: { color: col.text },
      formatter: (params) => tooltipHtml(dash, params[0].dataIndex),
    },
    xAxis: {
      type: 'category',
      data: dates,
      boundaryGap: false,
      axisLabel: { color: col.muted, formatter: (v) => v.slice(5) },
      axisLine: { lineStyle: { color: col.grid } },
    },
    dataZoom: [
      { type: 'inside', ...zoom },
      {
        type: 'slider', ...zoom, height: 18, bottom: 10,
        borderColor: col.grid, textStyle: { color: col.muted },
        labelFormatter: (_, v) => (v ? v.slice(5) : ''),
      },
    ],
  };
}

function weightOption(dash, col) {
  const s = dash.series;
  const bandWidth = s.band_high.map((h, i) => (h == null ? null : +(h - s.band_low[i]).toFixed(2)));
  const markLineData = s.dates.includes(dash.today) ? [{ xAxis: dash.today }] : [];
  return {
    ...baseOption(dash, col, true),
    legend: {
      top: 0,
      textStyle: { color: col.muted },
      data: ['计划曲线', '每日称重', '趋势体重', '当前预测'],
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: { color: col.muted },
      splitLine: { lineStyle: { color: col.grid } },
    },
    series: [
      {
        name: 'band-base', type: 'line', data: s.band_low, stack: 'band',
        symbol: 'none', lineStyle: { opacity: 0 }, silent: true,
      },
      {
        name: '容差带', type: 'line', data: bandWidth, stack: 'band',
        symbol: 'none', lineStyle: { opacity: 0 }, areaStyle: { color: col.band }, silent: true,
      },
      {
        name: '计划曲线', type: 'line', data: s.plan, smooth: true, symbol: 'none',
        lineStyle: { type: 'dashed', width: 2, color: col.plan }, itemStyle: { color: col.plan },
        markLine: {
          symbol: 'none', silent: true, data: markLineData,
          label: { formatter: '今天', color: col.muted },
          lineStyle: { color: col.muted, type: 'solid', opacity: 0.5 },
        },
        markPoint: {
          symbol: 'pin', symbolSize: 40, itemStyle: { color: col.plan },
          label: { formatter: '目标', fontSize: 10, color: '#fff' },
          data: [{ coord: [dash.plan.target_date, dash.plan.target_weight] }],
        },
      },
      {
        name: '每日称重', type: 'scatter', data: s.actual, symbolSize: 6,
        itemStyle: { color: col.actual, opacity: 0.8 },
      },
      {
        name: '趋势体重', type: 'line', data: s.trend, smooth: true, connectNulls: true, symbol: 'none',
        lineStyle: { width: 3, color: col.trend }, itemStyle: { color: col.trend },
      },
      {
        name: '当前预测', type: 'line', data: s.projection, smooth: true, symbol: 'none',
        lineStyle: { type: 'dotted', width: 2, color: col.proj }, itemStyle: { color: col.proj },
      },
    ],
  };
}

function calorieOption(dash, col) {
  const s = dash.series;
  return {
    ...baseOption(dash, col, false),
    legend: { top: 0, textStyle: { color: col.muted }, data: ['预计消耗', '建议摄入', '热量缺口'] },
    yAxis: {
      type: 'value',
      axisLabel: { color: col.muted },
      splitLine: { lineStyle: { color: col.grid } },
    },
    series: [
      { name: '热量缺口', type: 'bar', data: s.deficit, barMaxWidth: 8, itemStyle: { color: col.deficit } },
      {
        name: '预计消耗', type: 'line', data: s.burn, smooth: true, symbol: 'none',
        lineStyle: { width: 2, color: col.burn }, itemStyle: { color: col.burn },
      },
      {
        name: '建议摄入', type: 'line', data: s.intake, step: 'end', symbol: 'none',
        lineStyle: { width: 2, color: col.intake }, itemStyle: { color: col.intake },
      },
    ],
  };
}

function renderCharts(dash) {
  if (!dash || !dash.ready) return;
  const col = themeColors();
  weightChart ??= echarts.init($('#weightChart'));
  calChart ??= echarts.init($('#calChart'));
  weightChart.setOption(weightOption(dash, col), true);
  calChart.setOption(calorieOption(dash, col), true);
  weightChart.group = 'weight-planner';
  calChart.group = 'weight-planner';
  echarts.connect('weight-planner');
}

function renderCheckins(dash) {
  const panel = $('#checkinPanel');
  const rows = dash.ready ? dash.checkins : [];
  panel.hidden = rows.length === 0;
  $('#checkins tbody').innerHTML = rows
    .slice()
    .reverse()
    .map((c) => {
      let rec = `${kcal(c.recommended_intake)} kcal`;
      if (c.reached) rec += '（已达标·维持）';
      else if (c.recommended_intake > c.required_intake) rec += '（已按安全下限）';
      return `<tr><td>${esc(c.day)}</td><td>${fmt(c.trend_weight, 2)} kg</td><td>${rec}</td>`
        + `<td>${kcal(c.required_intake)} kcal</td><td>${c.days_left}</td></tr>`;
    })
    .join('');
}

function renderPlanSummary(dash) {
  const box = $('#planSummary');
  if (!dash.ready) {
    box.textContent = '';
    return;
  }
  const p = dash.plan;
  const parts = [
    `起始 ${fmt(p.start_weight, 2)} kg → 目标 ${fmt(p.target_weight)} kg，共 ${p.days} 天`,
    `按期达成需每天摄入约 ${kcal(p.intake)} kcal（安全下限 ${kcal(p.intake_floor)} kcal）`,
  ];
  if (p.losing) parts.push(`开始阶段每周约减体重的 ${fmt(p.weekly_loss_pct, 2)}%`);
  parts.push(`目标 BMI ${fmt(p.target_bmi)}`);
  box.textContent = `${parts.join('；')}。`;
}

function renderHistory(rows) {
  const more = $('#moreHistory');
  more.hidden = historyLimit === HISTORY_SHORT && rows.length < HISTORY_SHORT;
  more.textContent = historyLimit === HISTORY_SHORT ? '显示更多' : '收起';
  $('#history tbody').innerHTML = rows.length
    ? rows.map((r) => `<tr>
        <td>${esc(r.measured_at.slice(0, 16).replace('T', ' '))}</td>
        <td>${fmt(r.weight_kg, 2)}</td>
        <td>${esc(SOURCE[r.source] ?? r.source)}</td>
        <td><button class="link" data-del="${r.id}">删除</button></td>
      </tr>`).join('')
    : '<tr><td colspan="4" class="muted">还没有记录</td></tr>';
}

function renderProfile(profile) {
  const form = $('#profileForm');
  const values = profile ?? {
    sex: 'male',
    activity: 'light',
    plan_start: todayIso(),
    target_date: isoAddDays(todayIso(), 90),
    exercise_kcal: 0,
  };
  for (const el of form.elements) {
    if (el.name && el.name in values) el.value = values[el.name] ?? '';
  }
}

function renderDashboard(dash) {
  lastDash = dash;
  $('#todayLabel').textContent = dash.today ?? todayIso();
  renderWarnings(dash);
  renderEmpty(dash);
  $('#dashboard').hidden = !dash.ready;
  if (dash.ready) {
    renderCard(dash);
    renderCharts(dash);
  }
  renderCheckins(dash);
  renderPlanSummary(dash);
}

async function refresh() {
  const [dash, profile, rows] = await Promise.all([
    api('/api/dashboard'),
    api('/api/profile'),
    api(`/api/weighins?limit=${historyLimit}`),
  ]);
  renderProfile(profile);
  renderHistory(rows);
  renderDashboard(dash);
}

// ---------- 交互 ----------

$('#weighForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const msg = $('#weighMsg');
  const body = { weight_kg: Number(form.weight_kg.value) };
  if (form.measured_at.value) body.measured_at = form.measured_at.value;
  try {
    await api('/api/weighins', { method: 'POST', body: JSON.stringify(body) });
    form.weight_kg.value = '';
    form.measured_at.value = nowLocalInput();
    setMsg(msg, '已保存');
    await refresh();
  } catch (err) {
    setMsg(msg, `保存失败：${err.message}`, 'error');
  }
});

$('#importBtn').addEventListener('click', async () => {
  const msg = $('#weighMsg');
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

$('#history').addEventListener('click', async (e) => {
  const id = e.target.dataset.del;
  if (!id || !confirm('删除这条记录？')) return;
  try {
    await api(`/api/weighins/${id}`, { method: 'DELETE' });
    await refresh();
  } catch (err) {
    setMsg($('#weighMsg'), `删除失败：${err.message}`, 'error');
  }
});

$('#moreHistory').addEventListener('click', async () => {
  historyLimit = historyLimit === HISTORY_SHORT ? HISTORY_LONG : HISTORY_SHORT;
  try {
    renderHistory(await api(`/api/weighins?limit=${historyLimit}`));
  } catch (err) {
    setMsg($('#weighMsg'), `加载失败：${err.message}`, 'error');
  }
});

$('#profileForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target;
  const msg = $('#profileMsg');
  const body = {
    sex: f.sex.value,
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
    setMsg(msg, '计划已保存');
    await refresh();
  } catch (err) {
    setMsg(msg, `保存失败：${err.message}`, 'error');
  }
});

window.addEventListener('resize', () => {
  weightChart?.resize();
  calChart?.resize();
  renderCharts(lastDash); // 跨过窄屏断点时重新排版图例
});

window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => renderCharts(lastDash));

$('#weighForm').measured_at.value = nowLocalInput();
refresh().catch((err) => {
  $('#warnings').innerHTML = `<div class="alert danger">加载失败：${esc(err.message)}</div>`;
});
