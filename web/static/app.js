// Sentinel dashboard: refresh server-rendered fragments and draw the price chart.
// No external libraries, so it works offline (apart from the agent's own Deriv feed).
"use strict";

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const SVG = "http://www.w3.org/2000/svg";

function el(tag, attrs, parent) {
  const n = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
}

// ---------------------------------------------------------------- fragments
async function refresh(node) {
  try {
    const res = await fetch(node.dataset.src, { cache: "no-store" });
    if (res.ok) node.innerHTML = await res.text();
  } catch (_) { /* keep the last good content */ }
}
document.querySelectorAll("[data-src]").forEach((node) => {
  refresh(node);
  setInterval(() => refresh(node), Number(node.dataset.every || 5000));
});

// ---------------------------------------------------------------- price chart
const myt = (epoch) =>
  new Date(epoch * 1000).toLocaleTimeString("en-GB", { timeZone: "Asia/Kuala_Lumpur" });

function drawChart(box, data) {
  const W = box.clientWidth || 800, H = box.clientHeight || 280;
  const pad = { l: 64, r: 14, t: 12, b: 26 };
  box.innerHTML = "";
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" }, box);
  const ticks = data.ticks || [];
  if (ticks.length < 2) {
    const t = el("text", { x: W / 2, y: H / 2, "text-anchor": "middle", fill: css("--muted"), "font-size": 13 }, svg);
    t.textContent = "Waiting for live ticks from Deriv…";
    return;
  }
  const xs = ticks.map((p) => p[0]), ys = ticks.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const padY = (y1 - y0) * 0.08 || 1; y0 -= padY; y1 += padY;
  const X = (x) => pad.l + ((x - x0) / (x1 - x0 || 1)) * (W - pad.l - pad.r);
  const Y = (y) => pad.t + (1 - (y - y0) / (y1 - y0)) * (H - pad.t - pad.b);

  for (let i = 0; i <= 4; i++) {
    const v = y0 + ((y1 - y0) * i) / 4, y = Y(v);
    el("line", { x1: pad.l, x2: W - pad.r, y1: y, y2: y, stroke: css("--grid") }, svg);
    const t = el("text", { x: pad.l - 8, y: y + 4, "text-anchor": "end", fill: css("--muted"), "font-size": 11 }, svg);
    t.textContent = v.toFixed(2);
  }
  for (let i = 0; i <= 4; i++) {
    const v = x0 + ((x1 - x0) * i) / 4;
    const t = el("text", { x: X(v), y: H - 6, "text-anchor": i === 0 ? "start" : i === 4 ? "end" : "middle", fill: css("--muted"), "font-size": 11 }, svg);
    t.textContent = myt(v);
  }
  const d = ticks.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("");
  el("path", { d, fill: "none", stroke: css("--chart-line"), "stroke-width": 1.4 }, svg);

  for (const b of data.blocked || []) {
    if (b.epoch < x0) continue;
    const c = el("circle", { cx: X(b.epoch), cy: Y(b.quote), r: 5, fill: "none", stroke: css("--neg"), "stroke-width": 2 }, svg);
    el("title", {}, c).textContent = `${b.verdict === "REJECT" ? "Blocked" : "Needs human"}: ${b.action} at ${myt(b.epoch)}`;
  }
  for (const t of data.trades || []) {
    if (t.epoch < x0) continue;
    const up = t.contract_type === "CALL", x = X(t.epoch), y = Y(t.quote);
    const pts = up ? `${x},${y - 9} ${x - 7},${y + 4} ${x + 7},${y + 4}` : `${x},${y + 9} ${x - 7},${y - 4} ${x + 7},${y - 4}`;
    const m = el("polygon", { points: pts, fill: css(up ? "--call" : "--put"), stroke: css("--panel"), "stroke-width": 1 }, svg);
    el("title", {}, m).textContent = `${t.contract_type} at ${myt(t.epoch)} · ${t.status}${t.profit != null ? " " + t.profit.toFixed(2) : ""}`;
    m.style.cursor = "pointer";
    m.addEventListener("click", () => { location.href = `/decisions/${t.decision_id}`; });
  }
}

const chartBox = document.getElementById("chart");
if (chartBox) {
  const load = async () => {
    try {
      const res = await fetch("/api/chart", { cache: "no-store" });
      if (res.ok) drawChart(chartBox, await res.json());
    } catch (_) { /* keep last chart */ }
  };
  load();
  setInterval(load, 3000);
  window.addEventListener("resize", load);
}

// ---------------------------------------------------------------- decision sparkline
const spark = document.getElementById("spark");
if (spark) {
  const ticks = JSON.parse(spark.dataset.ticks || "[]");
  drawChart(spark, { ticks: ticks.map((q, i) => [i, q]), trades: [], blocked: [] });
  spark.querySelectorAll("text").forEach((t) => { if (/\d:\d/.test(t.textContent)) t.remove(); });
}
