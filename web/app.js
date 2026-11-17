"use strict";
/* Island ferry crowds — front end (vanilla JS + Apache ECharts). All times are Toronto time. */
const LEVELS = ["Quiet", "Moderate", "Busy", "Very busy"];
const LEVEL_HELP = ["plenty of room on the boats", "steady, still walk-on", "lines at the gates, boats filling up", "boats near full, you may wait for the next one"];
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const PERIOD_NAMES = { winter: "Winter", spring: "Spring", summer: "Summer", summer_peak: "Peak summer", fall: "Fall" };
const YEAR_COLORS = { light: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
                      dark: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9", "#e66767"] };
const VIRIDIS = ["#fde725", "#a0da39", "#4ac16d", "#1fa187", "#277f8e", "#365c8d", "#46327e", "#440154"];
const RAIN = ["#e8d8a0", "#9fd0b8", "#4fa3c8", "#2f5fa8", "#3b2a7a"];
const $ = (id) => document.getElementById(id);
const n0 = (x) => (x == null || isNaN(x) ? "–" : Math.round(x).toLocaleString("en-CA"));
const hh = (h) => (h === 24 ? "midnight" : h === 12 ? "noon" : `${h % 12 === 0 ? 12 : h % 12} ${h < 12 ? "am" : "pm"}`);
const fmtH = (h) => `${String(h % 24).padStart(2, "0")}:00`;
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const dark = () => matchMedia("(prefers-color-scheme: dark)").matches && document.documentElement.dataset.theme !== "light";
const lv = (i) => css(`--lv${i}`);
const seq = () => (dark() ? [...VIRIDIS].reverse() : [...VIRIDIS]);
const cache = new Map();
async function api(path) {
  if (cache.has(path)) return cache.get(path);
  const p = fetch(path, { headers: { Accept: "application/json" } }).then((r) => {
    if (!r.ok) throw new Error(`${path}: ${r.status}`);
    return r.json();
  });
  cache.set(path, p);
  p.catch(() => cache.delete(path));
  return p;
}
function torontoNow() {
  const p = new Intl.DateTimeFormat("en-CA", { timeZone: "America/Toronto", hour: "numeric", minute: "numeric", hourCycle: "h23" }).formatToParts(new Date());
  return +p.find((x) => x.type === "hour").value + +p.find((x) => x.type === "minute").value / 60;
}
const ago = (iso) => { const m = Math.round((Date.now() - new Date(iso)) / 60000); return m < 2 ? "just now" : m < 90 ? `${m} min ago` : `${Math.round(m / 60)} h ago`; };
const prettyDate = (s, o = { weekday: "short", month: "short", day: "numeric" }) => new Date(s + "T12:00:00").toLocaleDateString("en-CA", o);
const iso = (d) => new Intl.DateTimeFormat("en-CA", { timeZone: "America/Toronto", year: "numeric", month: "2-digit", day: "2-digit" }).format(d);
const torontoWall = () => `${iso(new Date())}T${new Intl.DateTimeFormat("en-GB", { timeZone: "America/Toronto", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date())}:00`;

/* ---------------- charts ---------------- */
const charts = new Map();
function chart(id) {
  let c = charts.get(id);
  if (!c || c.isDisposed()) { c = echarts.init($(id), null, { renderer: "canvas" }); charts.set(id, c); }
  return c;
}
addEventListener("resize", () => charts.forEach((c) => !c.isDisposed() && c.resize()));
function tip() {
  return { trigger: "axis", backgroundColor: css("--surface-3"), borderColor: css("--grid"), textStyle: { color: css("--ink") }, confine: true };
}
function base(extra = {}) {
  return {
    textStyle: { fontFamily: "Atkinson Hyperlegible, system-ui, sans-serif", color: css("--ink-2") },
    grid: { left: 56, right: 18, top: 30, bottom: 40 },
    tooltip: tip(),
    animation: !(innerWidth < 640 || matchMedia("(prefers-reduced-motion: reduce)").matches),
    animationDuration: 500,
    ...extra,
  };
}
const axis = (o = {}) => ({ axisLine: { lineStyle: { color: css("--grid") } }, axisTick: { show: false },
  splitLine: { lineStyle: { color: css("--grid") } }, axisLabel: { color: css("--ink-3") }, ...o });
function levelLegend(el) {
  $(el).innerHTML = LEVELS.map((l, i) => `<span><i style="background:${lv(i)}"></i><b>${l}</b>&nbsp;${LEVEL_HELP[i]}</span>`).join("");
}

/* ---------------- schedule banner ---------------- */
let SCHED = null;
function thresholds(periodKey) {
  const cap = SCHED.capacity_per_hour[periodKey] ?? SCHED.capacity_per_hour[periodKey.split("|")[0]];
  const s = SCHED.level_shares;
  return { cap, t: [cap * s.moderate, cap * s.busy, cap * s.very_busy] };
}
async function renderSchedule() {
  SCHED = await api("/api/schedule");
  const key = SCHED.today.period;
  const per = SCHED.periods.find((p) => p.key === (key === "summer_peak" ? "summer" : key));
  const nxt = SCHED.upcoming_changes[0];
  const md = (s) => new Date(`2001-${s}T12:00:00`).toLocaleDateString("en-CA", { month: "short", day: "numeric" });
  const { cap } = thresholds(SCHED.today.period_key);
  const routesShort = per.routes.length === 1 ? `${per.routes[0].route} only, boats ${per.routes[0].frequency}` : "all three routes";
  $("sched-line").innerHTML = `<b>${PERIOD_NAMES[key]} schedule:</b> ${routesShort}${nxt ? `. ${PERIOD_NAMES[nxt.period]} schedule from ${prettyDate(nxt.date, { month: "short", day: "numeric" })}` : ""}. <span class="det">Details</span>`;
  const routes = per.routes.map((r) => `<li><b>${r.route}</b>${r.frequency}${r.first ? `, ${r.first} to ${r.last}` : ""}</li>`).join("");
  const deps = per.routes.filter((r) => r.departures_from_city).map((r) =>
    `<p class="muted">${r.route} departures from the city:</p><ul class="deps">${r.departures_from_city.map((d) => `<li>${d}</li>`).join("")}</ul>`).join("");
  $("sched-body").innerHTML = `<p>${per.summary} ${md(per.from)} to ${md(per.to)}. The boats can carry about <b>${n0(cap)}</b> people an hour to the Island on this schedule.</p>
    <ul class="routes">${routes}</ul>${deps}<p class="muted">Exact times are set by the City each season: <a href="${SCHED.source_url}">official schedule</a>.</p>`;
  $("sched-full").innerHTML = `<p class="muted">${SCHED.periods.map((p) => `${p.name}: ${md(p.from)} to ${md(p.to)}. ${p.summary}`).join("<br>")}</p>
    <table class="fleet"><thead><tr><th>Vessel</th><th>Built</th><th>Passengers</th><th></th></tr></thead><tbody>${
    SCHED.fleet.map((f) => `<tr><td>${f.vessel}</td><td>${f.built}</td><td>${n0(f.passengers)}</td><td class="muted">${f.note || ""}</td></tr>`).join("")}</tbody></table>`;
}

/* ---------------- today ---------------- */
let FC = null, BESTS = [];
const dayMax = (d) => Math.max(...d.hours.filter((h) => h.hour >= 9 && h.hour < 19).map((h) => h.level));
function headline(day, best, tomorrow, bestT) {
  const now = torontoNow();
  const remaining = day.hours.filter((h) => h.hour + 1 > now && h.hour >= 9 && h.hour < 19);
  if (dayMax(day) <= 1) return "No crowds expected today. Any boat should be fine.";
  const picks = best.best.map((w) => +w.start.slice(0, 2)).filter((h) => h + 1 > now).sort((a, b) => a - b);
  if (picks.length && remaining.length) return `Quietest today: ${picks.slice(0, 2).map(fmtH).join(" and ")}.`;
  if (!tomorrow) return "Today's busy hours are over.";
  if (dayMax(tomorrow) <= 1) return "Today's over. No crowds expected tomorrow either.";
  const t = bestT.best.map((w) => +w.start.slice(0, 2)).sort((a, b) => a - b).slice(0, 2).map(fmtH);
  return `Today's over. Tomorrow, go at ${t.join(" or ")}.`;
}
function zones(periodKey, ymax) {
  const { t } = thresholds(periodKey);
  const edges = [0, ...t, Math.max(ymax, t[2] * 1.05)];
  return LEVELS.map((l, i) => [{ yAxis: edges[i], name: l, itemStyle: { color: lv(i), opacity: 0.12 },
    label: { show: true, position: "insideTopRight", color: css("--ink-3"), fontSize: 11 } }, { yAxis: Math.min(edges[i + 1], ymax) }])
    .filter((z) => z[0].yAxis < ymax);
}
function todayOption(withZones) {
  const day = FC.days[0];
  const hrs = day.hours;
  const ymax = Math.ceil(Math.max(50, ...hrs.map((h) => Math.max(h.p90, h.typical || 0, h.observed || 0))) * 1.12);
  const nowIdx = torontoNow() - 6;
  const periodKey = SCHED ? SCHED.today.period_key : "summer";
  return base({
    tooltip: { ...tip(), formatter: (ps) => {
      const h = hrs[ps[0].dataIndex];
      return `<b>${fmtH(h.hour)}–${fmtH(h.hour + 1)}</b><br>${LEVELS[h.level]}: about ${n0(h.pred)} people<br>Likely ${n0(h.p10)}–${n0(h.p90)}` +
        (h.typical != null ? `<br>Typical: ${n0(h.typical)}` : "") + (h.observed != null ? `<br>Counted: ${n0(h.observed)}` : "") +
        (h.temp_c != null ? `<br>${Math.round(h.temp_c)}°C${h.precip_mm >= 0.2 ? `, ${h.precip_mm} mm rain` : ""}` : "");
    } },
    grid: { left: 44, right: 14, top: 14, bottom: 30 },
    xAxis: axis({ type: "category", data: hrs.map((h) => hh(h.hour)), boundaryGap: false }),
    yAxis: axis({ type: "value", max: ymax, splitNumber: 4, axisLabel: { color: css("--ink-3"), showMaxLabel: false } }),
    series: [
      { type: "line", data: hrs.map((h) => h.p10), stack: "band", symbol: "none", lineStyle: { opacity: 0 }, tooltip: { show: false } },
      { type: "line", data: hrs.map((h) => h.p90 - h.p10), stack: "band", symbol: "none", lineStyle: { opacity: 0 }, areaStyle: { color: css("--band") },
        markArea: withZones ? { silent: true, data: zones(periodKey, ymax) } : undefined },
      { type: "line", data: hrs.map((h) => h.typical), symbol: "none", lineStyle: { width: 1.5, color: css("--typical") } },
      { type: "line", data: hrs.map((h) => ({ value: h.pred, itemStyle: { color: lv(h.level) } })), symbol: "circle", symbolSize: 7, showAllSymbol: true,
        lineStyle: { width: 2.5, color: css("--line") },
        markLine: nowIdx >= 0 && nowIdx <= 17 ? { silent: true, symbol: "none", lineStyle: { color: css("--ink-3"), type: "solid" }, label: { formatter: "now", color: css("--ink-3") }, data: [{ xAxis: Math.floor(nowIdx) }] } : undefined },
      { type: "scatter", data: hrs.map((h) => h.observed), symbolSize: 8, itemStyle: { color: css("--ink"), borderColor: css("--surface"), borderWidth: 2 } },
    ],
  });
}
/* main forecast chart: Day / Week / Month */
let range = ["day", "week", "month"].includes(new URLSearchParams(location.search).get("range")) ? new URLSearchParams(location.search).get("range") : "day";
const swatch = (cls, text) => `<span><i class="sw ${cls}"></i>${text}</span>`;
const levelDots = () => LEVELS.map((l, i) => `<span><i class="sw" style="background:${lv(i)};width:10px;height:10px;border-radius:50%"></i>${l}</span>`).join("");
function weekSimpleOption() {
  const pts = [];
  FC.days.slice(0, 7).forEach((d) => d.hours.forEach((h) => pts.push({ t: `${d.date}T${String(h.hour).padStart(2, "0")}:30:00`, ...h })));
  return base({
    tooltip: { ...tip(), formatter: (ps) => { const p = pts[ps[0].dataIndex]; return p ? `<b>${prettyDate(p.t.slice(0, 10))}, ${fmtH(p.hour)}</b><br>${LEVELS[p.level]}: about ${n0(p.pred)} people<br>Likely ${n0(p.p10)}–${n0(p.p90)}` : ""; } },
    grid: { left: 44, right: 14, top: 14, bottom: 30 },
    xAxis: axis({ type: "time", splitLine: { show: true, lineStyle: { color: css("--grid") } }, minInterval: 864e5,
      axisLabel: { color: css("--ink-3"), formatter: (v) => new Date(v).toLocaleDateString("en-CA", { weekday: "short", day: "numeric" }) } }),
    yAxis: axis({ type: "value", splitNumber: 4, axisLabel: { color: css("--ink-3"), showMaxLabel: false } }),
    series: [
      { type: "line", data: pts.map((p) => [p.t, p.p10]), stack: "b", symbol: "none", lineStyle: { opacity: 0 }, tooltip: { show: false } },
      { type: "line", data: pts.map((p) => [p.t, p.p90 - p.p10]), stack: "b", symbol: "none", lineStyle: { opacity: 0 }, areaStyle: { color: css("--band") } },
      { type: "line", data: pts.map((p) => ({ value: [p.t, p.pred], itemStyle: { color: lv(p.level) } })), symbol: "circle", symbolSize: 5, showAllSymbol: true,
        lineStyle: { width: 2, color: css("--line") },
        markLine: { silent: true, symbol: "none", lineStyle: { color: css("--ink-3"), type: "solid" }, label: { formatter: "now", color: css("--ink-3") }, data: [{ xAxis: torontoWall() }] } },
    ],
  });
}
function monthSimpleOption() {
  const all = monthDays().slice(0, 30);
  return base({
    tooltip: { ...tip(), formatter: (ps) => { const d = all[ps[0].dataIndex];
      return `<b>${prettyDate(d.date, { weekday: "long", month: "long", day: "numeric" })}</b>${d.holiday && d.holiday !== "none" ? " (holiday)" : ""}<br>About ${n0(d.total)} people (likely ${n0(d.total_p10)}–${n0(d.total_p90)})<br>Busiest hour: ${LEVELS[d.peak_level]}<br>${d.detailed ? "Weather-based forecast" : "Typical for the date; no weather yet"}`; } },
    grid: { left: 52, right: 14, top: 14, bottom: 44 },
    xAxis: axis({ type: "category", data: all.map((d) => d.date),
      axisLabel: { color: css("--ink-3"), interval: 0, formatter: (v) => { const dt = new Date(v + "T12:00:00"); return dt.getDay() === 1 || v === all[0].date ? dt.toLocaleDateString("en-CA", { month: "short", day: "numeric" }) : ""; } } }),
    yAxis: axis({ type: "value", splitNumber: 4, max: Math.ceil(Math.max(...all.map((d) => d.total_p90)) * 1.02), axisLabel: { color: css("--ink-3"), showMaxLabel: false } }),
    series: [
      { type: "bar", barMaxWidth: 16, data: all.map((d) => ({ value: d.total, itemStyle: { color: lv(d.peak_level), opacity: d.detailed ? 1 : 0.75, borderRadius: [3, 3, 0, 0] } })) },
      { type: "custom", z: 3, tooltip: { show: false }, data: all.map((d, i) => [i, d.total_p10, d.total_p90]),
        renderItem: (params, a) => { const lo = a.coord([a.value(0), a.value(1)]), hi = a.coord([a.value(0), a.value(2)]);
          return { type: "line", shape: { x1: lo[0], y1: lo[1], x2: hi[0], y2: hi[1] }, style: { stroke: css("--ink-3"), lineWidth: 1 } }; } },
    ],
  });
}
async function renderMain() {
  const cap = $("chart-cap"), leg = $("chart-legend");
  if (range === "day") {
    cap.textContent = "Today, hour by hour";
    leg.innerHTML = swatch("line", "Expected boardings per hour") + swatch("band", "Likely range") + swatch("typ", "Typical for this time of year") + swatch("dot", "Counted so far");
    chart("c-today").setOption(todayOption(false), true);
  } else if (range === "week") {
    cap.textContent = "Next 7 days, hour by hour";
    leg.innerHTML = swatch("line", "Expected boardings per hour") + swatch("band", "Likely range") + levelDots();
    chart("c-today").setOption(weekSimpleOption(), true);
  } else {
    cap.textContent = "Next 30 days, day by day";
    if (!OUT) OUT = await api("/api/outlook");
    leg.innerHTML = `<span>Bars: expected boardings per day, coloured by the busiest hour.</span>` + levelDots() +
      `<span>Lines: likely range. After the first week, bars show what is typical for the date (no weather yet).</span>`;
    chart("c-today").setOption(monthSimpleOption(), true);
  }
}
function renderToday() {
  const d = FC.days[0];
  const pretty = prettyDate(d.date, { weekday: "long", month: "long", day: "numeric" });
  $("today-date").textContent = d.date === iso(new Date()) ? `Today, ${pretty}` : `Latest forecast: ${pretty} (a new one is on its way)`;
  renderMain().catch(showError);
}

function renderFacts() {
  api("/api/now").then((n) => {
    $("now").hidden = false;
    $("now").innerHTML = n.available
      ? `<span class="muted">Right now (${n.hour_start}–${n.hour_end}; counts lag about an hour)</span><b>${n0(n.observed_boardings)} boarded</b><span>${n.label === "unknown" ? "" : `${n.label[0].toUpperCase()}${n.label.slice(1)} for this time of year`}</span>`
      : `<span class="muted">Right now</span><b>No boardings counted yet today</b>`;
  }).catch(() => { $("now").hidden = true; });
  api(`/api/return-estimate?date=${FC.days[0].date}`).then((r) => {
    $("ret").innerHTML = `<h3>Coming back</h3><p>${r.message || "Return lines should stay manageable."} <span class="muted">A rough estimate: nobody counts return trips.</span></p>`;
  }).catch(() => {});
}

/* ---------------- week ---------------- */
function renderWeekList() {
  const days = FC.days.slice(0, 8);
  const ol = $("week-list");
  ol.replaceChildren();
  days.forEach((d, i) => {
    const li = document.createElement("li");
    li.className = "frow";
    const mx = Math.max(...d.hours.filter((h) => h.hour >= 9 && h.hour < 19).map((h) => h.level));
    const name = d.lead === 0 ? "Today" : d.lead === 1 ? "Tmrw" : DAYS[d.dow];
    const conf = d.lead <= 1 ? "" : d.lead <= 3 ? "fair confidence" : "rough guess";
    const b = BESTS[i];
    const info = mx === 0 ? `<span>Plenty of room all day</span>` : mx === 1 ? `<span>Steady at times, still walk-on</span>` :
      `<span>Best <b>${b.best.slice(0, 2).map((w) => w.start).sort().join(", ")}</b></span><span>Busiest ${b.worst[0].start}</span>`;
    li.innerHTML = `<div class="d">${name}<small>${prettyDate(d.date, { month: "short", day: "numeric" })}${d.is_holiday ? ", holiday" : ""}</small></div>
      <span class="chip" title="Busyness at the busiest hour"><i style="background:${lv(mx)}"></i>${LEVELS[mx]}</span>
      <div class="info">${info}<span>${n0(d.total_p10)}–${n0(d.total_p90)} people</span>${conf ? `<span class="conf">${conf}</span>` : ""}</div>
      <div class="strip ${d.lead >= 4 ? "rough" : ""}" role="img" aria-label="Busyness by hour on ${d.date}">${d.hours.map((h) => `<i class="l${h.level}" title="${fmtH(h.hour)} ${LEVELS[h.level]}, about ${n0(h.pred)}"></i>`).join("")}</div>`;
    ol.appendChild(li);
  });
  levelLegend("levels");
}
function renderWeek() {
  const days = FC.days.slice(0, 8);
  const pts = [];
  days.forEach((d) => d.hours.forEach((h) => pts.push({ t: `${d.date}T${String(h.hour).padStart(2, "0")}:30:00`, ...h })));
  chart("c-week").setOption(base({
    tooltip: { ...tip(), formatter: (ps) => {
      const p = pts[ps[0].dataIndex];
      return p ? `<b>${prettyDate(p.t.slice(0, 10))}, ${fmtH(p.hour)}</b><br>${LEVELS[p.level]}: about ${n0(p.pred)}<br>Likely ${n0(p.p10)}–${n0(p.p90)}` : "";
    } },
    grid: { left: 56, right: 18, top: 36, bottom: 70 },
    xAxis: axis({ type: "time", splitLine: { show: false } }),
    yAxis: axis({ type: "value", name: "per hour", nameTextStyle: { color: css("--ink-3"), align: "right" } }),
    dataZoom: [{ type: "inside" }, { type: "slider", bottom: 12, height: 22, borderColor: css("--grid"), textStyle: { color: css("--ink-3") } }],
    series: [
      { type: "line", data: pts.map((p) => [p.t, p.p10]), stack: "b", symbol: "none", lineStyle: { opacity: 0 }, tooltip: { show: false } },
      { type: "line", data: pts.map((p) => [p.t, p.p90 - p.p10]), stack: "b", symbol: "none", lineStyle: { opacity: 0 }, areaStyle: { color: css("--band") } },
      { type: "line", name: "Forecast", data: pts.map((p) => [p.t, p.pred, p.level]), symbol: "circle", symbolSize: 6, showAllSymbol: true,
        lineStyle: { width: 2, color: css("--ink-3") }, itemStyle: { color: (q) => lv(q.data[2]), borderColor: css("--surface"), borderWidth: 1 } },
    ],
  }), true);

  chart("c-week-totals").setOption(base({
    tooltip: { ...tip(), formatter: (ps) => { const d = days[ps[0].dataIndex]; return `<b>${prettyDate(d.date)}</b><br>About ${n0(d.total)} boardings<br>Likely ${n0(d.total_p10)}–${n0(d.total_p90)}`; } },
    xAxis: axis({ type: "category", data: days.map((d) => prettyDate(d.date)) }),
    yAxis: axis({ type: "value", max: Math.ceil(Math.max(...days.map((d) => d.total_p90)) * 1.05) }),
    series: [
      { type: "bar", data: days.map((d) => ({ value: d.total, itemStyle: { color: lv(Math.max(...d.hours.map((h) => h.level))), borderRadius: [4, 4, 0, 0] } })), barMaxWidth: 26 },
      { type: "custom", renderItem: (params, a) => {
          const lo = a.coord([a.value(0), a.value(1)]), hi = a.coord([a.value(0), a.value(2)]);
          const s = { stroke: css("--ink-2"), lineWidth: 1.5 };
          return { type: "group", children: [{ type: "line", shape: { x1: lo[0], y1: lo[1], x2: hi[0], y2: hi[1] }, style: s },
            { type: "line", shape: { x1: lo[0] - 5, y1: lo[1], x2: lo[0] + 5, y2: lo[1] }, style: s },
            { type: "line", shape: { x1: hi[0] - 5, y1: hi[1], x2: hi[0] + 5, y2: hi[1] }, style: s }] };
        }, data: days.map((d, i) => [i, d.total_p10, d.total_p90]), tooltip: { show: false }, z: 3 },
    ],
  }), true);
  chart("c-today-cap").setOption(todayOption(true), true);
}

/* ---------------- months ---------------- */
let OUT = null, monthsColor = "level";
function monthDays() {
  const near = FC.days.map((d) => ({ date: d.date, total: d.total, total_p10: d.total_p10, total_p90: d.total_p90,
    peak_level: Math.max(...d.hours.map((h) => h.level)), hours: d.hours, holiday: d.holiday, detailed: true }));
  const seen = new Set(near.map((d) => d.date));
  return near.concat(OUT.days.filter((d) => !seen.has(d.date)));
}
function renderMonths() {
  const all = monthDays();
  const narrow = innerWidth < 640;
  const maxT = Math.max(...all.map((d) => d.total));
  const vm = monthsColor === "level"
    ? { type: "piecewise", dimension: 2, orient: "horizontal", left: "center", bottom: 0, textStyle: { color: css("--ink-2") },
        pieces: LEVELS.map((l, i) => ({ value: i, label: l, color: lv(i) })) }
    : { type: "continuous", dimension: 1, min: 0, max: Math.round(maxT), orient: "horizontal", left: "center", bottom: 0, calculable: true,
        inRange: { color: seq() }, textStyle: { color: css("--ink-2") }, text: ["more", "fewer boardings"] };
  chart("c-months-cal").setOption({
    ...base(),
    tooltip: { ...tip(), trigger: "item", formatter: (p) => {
      const d = all.find((x) => x.date === p.data[0]);
      return `<b>${prettyDate(d.date, { weekday: "long", month: "long", day: "numeric" })}</b>${d.holiday && d.holiday !== "none" ? " (holiday)" : ""}<br>About ${n0(d.total)} boardings (likely ${n0(d.total_p10)}–${n0(d.total_p90)})<br>Busiest hour: ${LEVELS[d.peak_level]}${d.detailed ? "<br>Weather-based forecast" : ""}`;
    } },
    visualMap: vm,
    calendar: { range: [all[0].date, all[all.length - 1].date], orient: narrow ? "vertical" : "horizontal", top: narrow ? 30 : 40, left: narrow ? 40 : 50, right: 20, bottom: 60,
      cellSize: narrow ? ["auto", 16] : ["auto", "auto"], splitLine: { lineStyle: { color: css("--ink-3"), width: 1 } },
      itemStyle: { borderColor: css("--surface"), borderWidth: 2, color: css("--surface-2") },
      dayLabel: { firstDay: 1, color: css("--ink-3"), nameMap: ["S", "M", "T", "W", "T", "F", "S"] }, monthLabel: { color: css("--ink-2") }, yearLabel: { show: false } },
    series: [{ type: "heatmap", coordinateSystem: "calendar", data: all.map((d) => [d.date, d.total, d.peak_level]) }],
  }, true);
  const c = chart("c-months-cal");
  c.off("click");
  c.on("click", (p) => showDay(all.find((x) => x.date === p.data[0])));

  const weeks = new Map();
  all.forEach((d) => {
    const dt = new Date(d.date + "T12:00:00"); const mon = new Date(dt); mon.setDate(dt.getDate() - ((dt.getDay() + 6) % 7));
    const k = iso(mon);
    const w = weeks.get(k) || { k, total: 0, lo: 0, hi: 0, n: 0 };
    w.total += d.total; w.lo += d.total_p10; w.hi += d.total_p90; w.n += 1; weeks.set(k, w);
  });
  // daily ranges don't add up linearly; a week's range is narrower than the sum of daily ranges
  const W = [...weeks.values()].filter((w) => w.n === 7).map((w) => ({ ...w, lo: w.total - (w.total - w.lo) * 0.6, hi: w.total + (w.hi - w.total) * 0.6 }));
  chart("c-months-weeks").setOption(base({
    tooltip: { ...tip(), formatter: (ps) => { const w = W[ps[0].dataIndex]; return `<b>Week of ${prettyDate(w.k)}</b><br>About ${n0(w.total)} boardings<br>Likely ${n0(w.lo)}–${n0(w.hi)}`; } },
    xAxis: axis({ type: "category", data: W.map((w) => prettyDate(w.k, { month: "short", day: "numeric" })) }),
    yAxis: axis({ type: "value" }),
    dataZoom: [{ type: "inside" }],
    series: [
      { type: "line", data: W.map((w) => w.lo), stack: "r", symbol: "none", lineStyle: { opacity: 0 }, tooltip: { show: false } },
      { type: "line", data: W.map((w) => w.hi - w.lo), stack: "r", symbol: "none", lineStyle: { opacity: 0 }, areaStyle: { color: css("--band") } },
      { type: "bar", data: W.map((w) => w.total), barMaxWidth: 18, itemStyle: { color: css("--line"), borderRadius: [4, 4, 0, 0] } },
    ],
  }), true);

  const months = new Map();
  all.forEach((d) => {
    const k = d.date.slice(0, 7); const m = months.get(k) || { k, total: 0, n: 0, busiest: d, busyDays: 0 };
    m.total += d.total; m.n += 1; if (d.total > m.busiest.total) m.busiest = d; if (d.peak_level >= 2) m.busyDays += 1; months.set(k, m);
  });
  $("months-table").innerHTML = `<table><thead><tr><th>Month</th><th>Expected boardings</th><th>Days with busy hours</th><th>Busiest expected day</th></tr></thead><tbody>${
    [...months.values()].map((m) => `<tr><td>${new Date(m.k + "-15T12:00:00").toLocaleDateString("en-CA", { month: "long", year: "numeric" })}${m.n < 28 ? " (part)" : ""}</td><td>${n0(m.total)}</td><td>${m.busyDays}</td><td>${prettyDate(m.busiest.date)} (${n0(m.busiest.total)})</td></tr>`).join("")}</tbody></table>`;
}
function showDay(d) {
  if (!d) return;
  const el = $("months-day");
  el.hidden = false;
  el.innerHTML = `<h3>${prettyDate(d.date, { weekday: "long", month: "long", day: "numeric", year: "numeric" })}</h3>
    <p class="note">About ${n0(d.total)} boardings, likely ${n0(d.total_p10)}–${n0(d.total_p90)}. ${d.detailed ? "Weather-based forecast." : "Typical for the date; no weather yet."}</p><div class="chart short" id="c-months-daychart"></div>`;
  const old = charts.get("c-months-daychart"); if (old) old.dispose();
  chart("c-months-daychart").setOption(base({
    xAxis: axis({ type: "category", data: d.hours.map((h) => hh(h.hour)) }), yAxis: axis({ type: "value" }),
    tooltip: { ...tip(), formatter: (ps) => { const h = d.hours[ps[0].dataIndex]; return `<b>${fmtH(h.hour)}</b><br>${LEVELS[h.level]}: about ${n0(h.pred)}`; } },
    series: [{ type: "bar", data: d.hours.map((h) => ({ value: h.pred, itemStyle: { color: lv(h.level), borderRadius: [3, 3, 0, 0] } })), barMaxWidth: 22 }],
  }), true);
  el.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* ---------------- explore ---------------- */
const EXCLUDED = [["2015-05-01", "2015-07-12", "Ticketing roll-out"], ["2017-05-01", "2017-07-30", "Flood closure"], ["2020-03-14", "2021-06-30", "Pandemic"]];
let exAgg = "day";
function segInit(id, onChange, toggle = true) {
  $(id).addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (toggle) $(id).querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
    onChange(b.dataset.v);
  });
}
async function renderTimeline() {
  let s = $("ex-start").value, e = $("ex-end").value;
  if (exAgg === "hour") {
    const end = e ? new Date(e) : new Date();
    if (!s || (end - new Date(s)) / 864e5 > 60) { s = iso(new Date(end - 14 * 864e5)); $("ex-start").value = s; }
  }
  const q = new URLSearchParams({ agg: exAgg }); if (s) q.set("start", s); if (e) q.set("end", e);
  const r = await api(`/api/history?${q}`);
  const S = r.series;
  const showSales = $("ex-sales").checked;
  const t = (x) => x.t.replace(" ", "T");
  const kind = S.length > 120 ? "line" : "bar";
  const hasWx = exAgg === "hour" || exAgg === "day";
  const xi = hasWx ? [0, 1] : [0];
  chart("c-timeline").setOption({
    ...base(),
    legend: { top: 0, left: 0, textStyle: { color: css("--ink-2") } },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    grid: hasWx ? [{ left: 60, right: 18, top: 30, height: "52%" }, { left: 60, right: 18, top: "70%", height: "15%" }] : [{ left: 60, right: 18, top: 30, bottom: 60 }],
    xAxis: hasWx ? [axis({ type: "time", gridIndex: 0, axisLabel: { show: false }, splitLine: { show: false } }), axis({ type: "time", gridIndex: 1, splitLine: { show: false } })]
                 : [axis({ type: "time", splitLine: { show: false } })],
    yAxis: hasWx ? [axis({ type: "value", gridIndex: 0, name: "boardings", nameTextStyle: { color: css("--ink-3") } }),
                    axis({ type: "value", gridIndex: 1, name: exAgg === "hour" ? "°C" : "daily high °C", splitNumber: 2, nameTextStyle: { color: css("--ink-3") } })]
                 : [axis({ type: "value", name: "boardings", nameTextStyle: { color: css("--ink-3") } })],
    dataZoom: [{ type: "inside", xAxisIndex: xi }, { type: "slider", xAxisIndex: xi, bottom: 4, height: 20, borderColor: css("--grid"), textStyle: { color: css("--ink-3") } }],
    series: [
      { name: "Boardings", type: kind, data: S.map((x) => [t(x), x.boardings]), symbol: "none", sampling: "lttb", barMaxWidth: 14,
        itemStyle: { color: css("--line") }, lineStyle: { width: 1.5, color: css("--line") }, areaStyle: kind === "line" ? { color: css("--band") } : undefined,
        markArea: { silent: true, itemStyle: { color: css("--ink-3"), opacity: 0.1 }, label: { color: css("--ink-3"), fontSize: 10 },
          data: EXCLUDED.map(([a, b, n]) => [{ xAxis: a, name: n }, { xAxis: b }]) } },
      ...(showSales ? [{ name: "Ticket sales", type: "line", data: S.map((x) => [t(x), x.sales]), symbol: "none", lineStyle: { width: 1.2, color: lv(2) }, itemStyle: { color: lv(2) } }] : []),
      ...(hasWx ? [{ name: exAgg === "hour" ? "Temperature" : "Daily high", type: "line", xAxisIndex: 1, yAxisIndex: 1, symbol: "none",
        data: S.map((x) => [t(x), exAgg === "hour" ? x.temp_c : x.tmax]), lineStyle: { width: 1.2, color: lv(1) }, itemStyle: { color: lv(1) } }] : []),
    ],
  }, true);
  const dq = new URLSearchParams(); if (s) dq.set("start", s); if (e) dq.set("end", e);
  $("dl-daily").href = `/api/download?dataset=daily&${dq}`;
  $("dl-hourly").href = `/api/download?dataset=hourly&${dq}`;
}
async function renderYears() {
  const Y = (await api("/api/years")).years;
  const thisYear = new Date().getFullYear();
  chart("c-years").setOption(base({
    tooltip: { ...tip(), formatter: (ps) => { const y = Y[ps[0].dataIndex];
      return `<b>${y.year}${y.year === thisYear ? " (so far)" : ""}</b><br>${n0(y.boardings)} boardings${y.growth != null ? `<br>${y.growth >= 0 ? "+" : ""}${Math.round(y.growth * 100)}% vs ${y.year - 1}` : ""}<br>Busiest day: ${y.busiest_date ? prettyDate(y.busiest_date.slice(0, 10), { month: "short", day: "numeric" }) : "–"} (${n0(y.busiest_day)})${y.excluded_days ? `<br>${y.excluded_days} disrupted days` : ""}`; } },
    xAxis: axis({ type: "category", data: Y.map((y) => String(y.year)) }),
    yAxis: axis({ type: "value" }),
    series: [{ type: "bar", barMaxWidth: 34,
      data: Y.map((y) => ({ value: y.boardings, itemStyle: { color: y.excluded_days > 30 ? css("--typical") : css("--line"), opacity: y.year === thisYear ? 0.6 : 1, borderRadius: [4, 4, 0, 0] } })),
      label: { show: true, position: "top", color: css("--ink-3"), fontSize: 11, formatter: (p) => { const g = Y[p.dataIndex].growth; return g == null ? "" : `${g >= 0 ? "+" : ""}${Math.round(g * 100)}%`; } } }],
  }), true);
}
let cmpAgg = "week";
async function renderCompare() {
  const r = await api(`/api/compare-years?agg=${cmpAgg}`);
  const years = Object.keys(r.series).map(Number).sort();
  const cols = YEAR_COLORS[dark() ? "dark" : "light"];
  const last = years[years.length - 1];
  const on = new Set([2019, 2023, 2024, 2025, last]);
  const xs = cmpAgg === "month" ? r.x.map((m) => new Date(2001, m - 1, 15).toLocaleDateString("en-CA", { month: "short" })) : r.x.map((w) => `wk ${w}`);
  chart("c-compare").setOption(base({
    legend: { top: 0, left: 0, type: "scroll", data: years.map(String), textStyle: { color: css("--ink-2") }, selected: Object.fromEntries(years.map((y) => [String(y), on.has(y)])) },
    grid: { left: 60, right: 18, top: 40, bottom: 40 },
    xAxis: axis({ type: "category", data: xs }), yAxis: axis({ type: "value" }),
    series: years.map((y) => ({ name: String(y), type: "line", symbol: "none", data: r.series[String(y)],
      lineStyle: { width: y === last ? 3 : 2, color: cols[(y - 2015) % 8] }, itemStyle: { color: cols[(y - 2015) % 8] } })),
  }), true);
}
async function renderHeatmap() {
  const q = new URLSearchParams(); if ($("hm-period").value) q.set("period", $("hm-period").value); if ($("hm-years").value) q.set("years", $("hm-years").value);
  const r = await api(`/api/heatmap?${q}`);
  const data = []; let mx = 0;
  r.values.forEach((row, i) => row.forEach((v, j) => { data.push([j, i, v]); if (v > mx) mx = v; }));
  chart("c-heatmap").setOption({
    ...base(), tooltip: { ...tip(), trigger: "item", formatter: (p) => `<b>${r.rows[p.data[1]]} ${fmtH(r.cols[p.data[0]])}</b><br>Average ${n0(p.data[2])} boardings` },
    grid: { left: 46, right: 18, top: 10, bottom: 70 },
    xAxis: axis({ type: "category", data: r.cols.map((h) => hh(h)) }),
    yAxis: axis({ type: "category", data: r.rows, inverse: true }),
    visualMap: { min: 0, max: Math.max(1, Math.round(mx)), calculable: true, orient: "horizontal", left: "center", bottom: 0, inRange: { color: seq() }, textStyle: { color: css("--ink-2") } },
    series: [{ type: "heatmap", data, itemStyle: { borderColor: css("--surface"), borderWidth: 2 } }],
  }, true);
}
async function renderCalendar() {
  const y = +$("cal-year").value;
  const r = await api(`/api/calendar?year=${y}`);
  const narrow = innerWidth < 640;
  const mx = Math.max(1, ...r.days.map((d) => d.boardings || 0));
  chart("c-calendar").setOption({
    ...base(), tooltip: { ...tip(), trigger: "item", formatter: (p) => { const d = r.days.find((x) => x.date === p.data[0]);
      return `<b>${prettyDate(d.date, { weekday: "long", month: "long", day: "numeric" })}</b>${d.holiday !== "none" ? " (holiday)" : ""}<br>${n0(d.boardings)} boardings<br>High ${d.tmax ?? "–"}°C, ${d.precip_mm ?? 0} mm rain${d.regime !== "normal" ? `<br>${d.regime.replace(/_/g, " ")}` : ""}`; } },
    visualMap: { min: 0, max: Math.round(mx), calculable: true, orient: "horizontal", left: "center", bottom: 0, inRange: { color: seq() }, textStyle: { color: css("--ink-2") } },
    calendar: { range: String(y), orient: narrow ? "vertical" : "horizontal", top: narrow ? 30 : 40, left: narrow ? 40 : 50, right: 20, bottom: 60, cellSize: narrow ? ["auto", 13] : ["auto", "auto"],
      splitLine: { lineStyle: { color: css("--ink-3") } }, itemStyle: { borderColor: css("--surface"), borderWidth: 2, color: css("--surface-2") },
      dayLabel: { firstDay: 1, color: css("--ink-3"), nameMap: ["S", "M", "T", "W", "T", "F", "S"] }, monthLabel: { color: css("--ink-2") }, yearLabel: { show: false } },
    series: [{ type: "heatmap", coordinateSystem: "calendar", data: r.days.filter((d) => d.boardings != null).map((d) => [d.date, d.boardings]) }],
  }, true);
}
async function renderHolidays() {
  const H = (await api("/api/holidays")).holidays.filter((h) => h.boardings != null);
  const order = ["New Year's Day", "Family Day", "Good Friday", "Victoria Day", "Canada Day", "Civic Holiday", "Labour Day", "Thanksgiving", "Christmas Day", "Boxing Day"]
    .filter((n) => H.some((h) => h.name === n));
  const years = [...new Set(H.map((h) => h.year))].sort();
  const mx = Math.max(...H.map((h) => h.boardings));
  chart("c-holidays").setOption({
    ...base(), tooltip: { ...tip(), trigger: "item", formatter: (p) => { const h = p.data[3];
      return `<b>${h.name} ${h.date}</b><br>${n0(h.boardings)} boardings${h.normal_day_nearby ? `<br>${(h.boardings / h.normal_day_nearby).toFixed(1)}× a normal day nearby (${n0(h.normal_day_nearby)})` : ""}<br>High ${h.tmax ?? "–"}°C, ${h.precip_mm ?? 0} mm rain`; } },
    grid: { left: 112, right: 18, top: 10, bottom: 70 },
    xAxis: axis({ type: "category", data: years.map(String) }), yAxis: axis({ type: "category", data: order, inverse: true }),
    visualMap: { min: 0, max: Math.round(mx), calculable: true, orient: "horizontal", left: "center", bottom: 0, inRange: { color: seq() }, textStyle: { color: css("--ink-2") } },
    series: [{ type: "heatmap", data: H.map((h) => [years.indexOf(h.year), order.indexOf(h.name), h.boardings, h]),
      label: { show: innerWidth > 700, formatter: (p) => `${(p.data[2] / 1000).toFixed(p.data[2] < 10000 ? 1 : 0)}k`, color: css("--ink"), fontSize: 10, textBorderColor: css("--surface"), textBorderWidth: 3 },
      itemStyle: { borderColor: css("--surface"), borderWidth: 2 } }],
  }, true);
}
let wxDay = "weekend";
async function renderWeather() {
  const r = await api(`/api/weather-effect?period=${$("wx-period").value}&daytype=${wxDay}`);
  const P = r.points.filter((p) => p.tmax != null && p.boardings != null);
  chart("c-weather").setOption(base({
    tooltip: { ...tip(), trigger: "item", formatter: (p) => { const d = P[p.dataIndex]; return `<b>${prettyDate(d.date, { weekday: "short", month: "short", day: "numeric", year: "numeric" })}</b><br>${n0(d.boardings)} boardings<br>High ${d.tmax}°C, ${d.precip_mm} mm rain`; } },
    grid: { left: 60, right: 18, top: 20, bottom: 80 },
    xAxis: axis({ type: "value", name: "daily high °C", nameLocation: "middle", nameGap: 26, scale: true, nameTextStyle: { color: css("--ink-3") } }),
    yAxis: axis({ type: "value", name: "boardings", nameTextStyle: { color: css("--ink-3") } }),
    visualMap: { type: "piecewise", dimension: 2, orient: "horizontal", left: "center", bottom: 0, textStyle: { color: css("--ink-2") },
      pieces: [{ max: 0.2, label: "dry", color: RAIN[0] }, { min: 0.2, max: 2, label: "light rain", color: RAIN[1] }, { min: 2, max: 8, label: "some rain", color: RAIN[2] },
               { min: 8, max: 20, label: "wet", color: RAIN[3] }, { min: 20, label: "very wet", color: RAIN[4] }] },
    series: [{ type: "scatter", symbolSize: 7, data: P.map((p) => [p.tmax, p.boardings, p.precip_mm]), itemStyle: { opacity: 0.85, borderColor: css("--surface"), borderWidth: 0.5 } }],
  }), true);
}

/* ---------------- about ---------------- */
async function renderAbout() {
  try {
    const a = await api("/api/accuracy");
    const b = a.by_lead && a.by_lead["same/next day"];
    if (b) $("acc").textContent = `Over the last ${a.window_days} days, the next-day forecast of the day's total was off by ${Math.round(b.median_daily_abs_pct_error * 100)}% on a typical day (${b.days_scored} days checked). The busyness level was right for ${Math.round(b.level_accuracy * 100)}% of hours.`;
  } catch (_) { /* accuracy not published yet */ }
  $("kpis").innerHTML = [
    ["13.5%", "typical miss on a day's total, forecast the day before (2024-26 backtest)"],
    ["37%", "less error than the best simple method, one day ahead"],
    ["90%", "of hours given the right busyness level"],
    ["11 years", "of 15-minute ticket counts behind it"],
  ].map(([v, l]) => `<div class="kpi"><b>${v}</b><span>${l}</span></div>`).join("");
}

/* ---------------- navigation ---------------- */
const rendered = new Set();
const SUBS = ["week", "months", "history", "about"];
const SUB_RENDER = {
  week: () => renderWeek(),
  months: async () => { OUT = await api("/api/outlook"); renderMonths(); },
  history: async () => {
    const yrs = (await api("/api/years")).years.map((y) => y.year);
    $("cal-year").innerHTML = yrs.slice().reverse().map((y) => `<option>${y}</option>`).join("");
    $("cal-year").value = String(yrs[yrs.length - 1] - (new Date().getMonth() < 9 ? 1 : 0));
    const end = new Date(); $("ex-end").value = iso(end); $("ex-start").value = iso(new Date(end - 365 * 864e5));
    await Promise.all([renderTimeline(), renderYears(), renderCompare(), renderHeatmap(), renderCalendar(), renderHolidays(), renderWeather()]);
  },
  about: () => renderAbout(),
};
let currentSub = "week";
function resizeAll() { requestAnimationFrame(() => charts.forEach((c) => !c.isDisposed() && c.resize())); }
function selectTab(name, push = true) {
  document.querySelectorAll('[role="tab"]').forEach((t) => t.setAttribute("aria-selected", t.id === `tab-${name}` ? "true" : "false"));
  document.querySelectorAll('[role="tabpanel"]').forEach((p) => (p.hidden = p.id !== `p-${name}`));
  $("range-seg").hidden = name !== "forecast";
  if (name === "deep") selectSub(currentSub, push);
  else if (push) history.replaceState(null, "", "#forecast");
  resizeAll();
}
function selectSub(name, push = true) {
  currentSub = name;
  document.querySelectorAll(".subnav button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.sub === name)));
  SUBS.forEach((s) => ($(`s-${s}`).hidden = s !== name));
  if (push) history.replaceState(null, "", `#${name}`);
  if (!rendered.has(name)) { rendered.add(name); Promise.resolve(SUB_RENDER[name]()).catch(showError); }
  resizeAll();
}
document.querySelector(".tabs").addEventListener("click", (e) => { const b = e.target.closest('[role="tab"]'); if (b) selectTab(b.id.slice(4)); });
document.querySelector(".tabs").addEventListener("keydown", (e) => {
  const tabs = [...document.querySelectorAll('[role="tab"]')]; const i = tabs.indexOf(document.activeElement);
  if (i < 0 || !["ArrowRight", "ArrowLeft"].includes(e.key)) return;
  const n = tabs[(i + 1) % tabs.length]; n.focus(); selectTab(n.id.slice(4));
});
document.querySelector(".subnav").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) selectSub(b.dataset.sub); });
$("go-deep").addEventListener("click", () => { selectTab("deep"); scrollTo({ top: 0, behavior: "smooth" }); });

segInit("ex-agg", (v) => { exAgg = v; renderTimeline().catch(showError); });
segInit("ex-preset", (v) => {
  const end = new Date(); $("ex-end").value = iso(end);
  $("ex-start").value = v === "all" ? "2015-05-01" : iso(new Date(end - +v * 864e5));
  if (v === "all" && exAgg === "hour") { exAgg = "week"; document.querySelectorAll("#ex-agg button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === "week"))); }
  renderTimeline().catch(showError);
}, false);
["ex-start", "ex-end", "ex-sales"].forEach((id) => $(id).addEventListener("change", () => renderTimeline().catch(showError)));
segInit("cmp-agg", (v) => { cmpAgg = v; renderCompare(); });
["hm-period", "hm-years"].forEach((id) => $(id).addEventListener("change", renderHeatmap));
$("cal-year").addEventListener("change", renderCalendar);
$("wx-period").addEventListener("change", renderWeather);
segInit("wx-day", (v) => { wxDay = v; renderWeather(); });
segInit("months-color", (v) => { monthsColor = v; renderMonths(); });
segInit("range-seg", (v) => {
  range = v;
  const u = new URL(location.href); if (v === "day") u.searchParams.delete("range"); else u.searchParams.set("range", v);
  history.replaceState(null, "", u);
  renderMain().catch(showError);
});
document.querySelectorAll("#range-seg button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === range)));

function showError(e) { console.error(e); }

async function main() {
  try {
    await renderSchedule().catch((e) => console.error(e));
    FC = await api("/api/forecast?days=8");
    BESTS = await Promise.all(FC.days.map((d) => api(`/api/best-times?date=${d.date}`)));
    $("headline").textContent = headline(FC.days[0], BESTS[0], FC.days[1], BESTS[1]);
    renderToday();
    renderFacts();
    renderWeekList();
    const at = new Intl.DateTimeFormat("en-CA", { timeZone: "America/Toronto", hour: "numeric", minute: "2-digit", timeZoneName: "short" }).format(new Date(FC.generated_at));
    const fresh = $("fresh");
    fresh.innerHTML = `<span class="dot"></span>Updated ${ago(FC.generated_at)} <span class="at">(${at})</span>`;
    fresh.classList.toggle("stale", (Date.now() - new Date(FC.generated_at)) / 60000 > 180);
    const h = (location.hash || "#forecast").slice(1);
    if (SUBS.includes(h)) { currentSub = h; selectTab("deep", false); } else selectTab("forecast", false);
  } catch (e) {
    $("headline").textContent = "The forecast isn't available right now.";
    $("fresh").textContent = "Offline";
    console.error(e);
  }
}
main();
setInterval(() => {
  for (const k of [...cache.keys()]) if (/\/api\/(forecast|best-times|now|return-estimate|outlook|schedule|accuracy)/.test(k)) cache.delete(k);
  OUT = null;
  ["week", "months", "about"].forEach((s) => rendered.delete(s));
  main();
}, 15 * 60 * 1000);
