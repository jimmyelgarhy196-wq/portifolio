/* ==========================================================================
   EGX ALPHA — canvas charting
   No external dependencies, so the terminal renders fully offline and behind
   restrictive networks. Handles price/volume, indicator panes and equity curves.
   ========================================================================== */
(function (global) {
  "use strict";

  const CSS = getComputedStyle(document.documentElement);
  const C = (name, fallback) => (CSS.getPropertyValue(name) || fallback).trim();

  const THEME = {
    grid: C("--border", "#1f2733"),
    axis: C("--text-faint", "#5a6474"),
    text: C("--text-dim", "#8b97a8"),
    up: C("--up", "#2fbf71"),
    down: C("--down", "#f0524d"),
    accent: C("--accent", "#46b1ff"),
    amber: C("--amber", "#f0b429"),
    panel: C("--bg-panel", "#10141c"),
    mono: '11px "SF Mono", Menlo, Consolas, monospace',
  };

  const SERIES_COLORS = [THEME.accent, THEME.amber, "#b18cf0", "#4fe08f", "#ff8b8b"];

  function dpi(canvas, cssHeight) {
    const ratio = global.devicePixelRatio || 1;
    const width = canvas.clientWidth || canvas.parentElement.clientWidth || 600;
    canvas.width = width * ratio;
    canvas.height = cssHeight * ratio;
    canvas.style.height = cssHeight + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    return { ctx, w: width, h: cssHeight };
  }

  function extent(values) {
    let lo = Infinity, hi = -Infinity;
    for (const v of values) {
      if (v === null || v === undefined || Number.isNaN(v)) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    if (lo === Infinity) return null;
    if (lo === hi) { lo -= 1; hi += 1; }
    return [lo, hi];
  }

  function fmtNum(v) {
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    const a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(1) + "B";
    if (a >= 1e6) return (v / 1e6).toFixed(1) + "M";
    if (a >= 1e3) return (v / 1e3).toFixed(1) + "K";
    if (a >= 100) return v.toFixed(0);
    if (a >= 1) return v.toFixed(2);
    return v.toFixed(3);
  }

  function drawGrid(ctx, box, yMin, yMax, ticks) {
    ctx.strokeStyle = THEME.grid;
    ctx.fillStyle = THEME.axis;
    ctx.font = THEME.mono;
    ctx.lineWidth = 1;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= ticks; i++) {
      const value = yMin + ((yMax - yMin) * i) / ticks;
      const y = Math.round(box.y + box.h - (box.h * i) / ticks) + 0.5;
      ctx.beginPath();
      ctx.moveTo(box.x, y);
      ctx.lineTo(box.x + box.w, y);
      ctx.stroke();
      ctx.fillText(fmtNum(value), box.x - 6, y);
    }
  }

  function drawDateAxis(ctx, box, dates) {
    if (!dates.length) return;
    ctx.fillStyle = THEME.axis;
    ctx.font = THEME.mono;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const count = Math.min(6, dates.length);
    for (let i = 0; i < count; i++) {
      const idx = Math.floor((dates.length - 1) * (i / Math.max(1, count - 1)));
      const x = box.x + (box.w * idx) / Math.max(1, dates.length - 1);
      const label = String(dates[idx]).slice(0, 7);
      ctx.fillText(label, Math.min(box.x + box.w - 18, Math.max(box.x + 18, x)), box.y + box.h + 5);
    }
  }

  function line(ctx, box, values, yMin, yMax, color, width) {
    ctx.strokeStyle = color;
    ctx.lineWidth = width || 1.4;
    ctx.beginPath();
    let started = false;
    const n = values.length;
    for (let i = 0; i < n; i++) {
      const v = values[i];
      if (v === null || v === undefined || Number.isNaN(v)) { started = false; continue; }
      const x = box.x + (box.w * i) / Math.max(1, n - 1);
      const y = box.y + box.h - ((v - yMin) / (yMax - yMin)) * box.h;
      if (!started) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, y); }
    }
    ctx.stroke();
  }

  function area(ctx, box, values, yMin, yMax, color) {
    const gradient = ctx.createLinearGradient(0, box.y, 0, box.y + box.h);
    gradient.addColorStop(0, color + "33");
    gradient.addColorStop(1, color + "03");
    ctx.fillStyle = gradient;
    ctx.beginPath();
    let started = false;
    const n = values.length;
    let lastX = box.x;
    for (let i = 0; i < n; i++) {
      const v = values[i];
      if (v === null || v === undefined || Number.isNaN(v)) continue;
      const x = box.x + (box.w * i) / Math.max(1, n - 1);
      const y = box.y + box.h - ((v - yMin) / (yMax - yMin)) * box.h;
      if (!started) { ctx.moveTo(x, box.y + box.h); ctx.lineTo(x, y); started = true; }
      else ctx.lineTo(x, y);
      lastX = x;
    }
    if (started) { ctx.lineTo(lastX, box.y + box.h); ctx.closePath(); ctx.fill(); }
  }

  function hline(ctx, box, value, yMin, yMax, color, dash) {
    if (value === null || value === undefined) return;
    const y = box.y + box.h - ((value - yMin) / (yMax - yMin)) * box.h;
    if (y < box.y || y > box.y + box.h) return;
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    if (dash) ctx.setLineDash(dash);
    ctx.beginPath();
    ctx.moveTo(box.x, y);
    ctx.lineTo(box.x + box.w, y);
    ctx.stroke();
    ctx.restore();
  }

  /* ---------------- Price chart with volume and overlays ---------------- */
  function priceChart(canvas, data, options) {
    options = options || {};
    const height = options.height || 320;
    const { ctx, w, h } = dpi(canvas, height);
    ctx.clearRect(0, 0, w, h);

    const closes = data.close || [];
    if (!closes.filter((v) => v !== null).length) {
      ctx.fillStyle = THEME.axis;
      ctx.font = THEME.mono;
      ctx.textAlign = "center";
      ctx.fillText("No price data available", w / 2, h / 2);
      return;
    }

    const padL = 52, padR = 10, padT = 8, padB = 20;
    const volH = options.showVolume === false ? 0 : Math.round(height * 0.2);
    const priceBox = { x: padL, y: padT, w: w - padL - padR, h: height - padT - padB - volH - (volH ? 8 : 0) };

    const overlays = options.overlays || [];
    let pool = closes.slice();
    overlays.forEach((o) => { pool = pool.concat(o.values || []); });
    (options.levels || []).forEach((l) => pool.push(l.value));
    const range = extent(pool);
    if (!range) return;
    const pad = (range[1] - range[0]) * 0.06;
    const yMin = range[0] - pad, yMax = range[1] + pad;

    drawGrid(ctx, priceBox, yMin, yMax, 4);

    const first = closes.find((v) => v !== null);
    const last = [...closes].reverse().find((v) => v !== null);
    const trendColor = last >= first ? THEME.up : THEME.down;
    area(ctx, priceBox, closes, yMin, yMax, trendColor);
    line(ctx, priceBox, closes, yMin, yMax, trendColor, 1.6);

    overlays.forEach((o, i) => {
      line(ctx, priceBox, o.values, yMin, yMax, o.color || SERIES_COLORS[i % SERIES_COLORS.length], 1.1);
    });
    (options.levels || []).forEach((l) => {
      hline(ctx, priceBox, l.value, yMin, yMax, l.color || THEME.axis, [4, 4]);
      const y = priceBox.y + priceBox.h - ((l.value - yMin) / (yMax - yMin)) * priceBox.h;
      if (y >= priceBox.y && y <= priceBox.y + priceBox.h && l.label) {
        ctx.fillStyle = l.color || THEME.axis;
        ctx.font = THEME.mono;
        ctx.textAlign = "left";
        ctx.textBaseline = "bottom";
        ctx.fillText(l.label, priceBox.x + 4, y - 2);
      }
    });

    if (volH) {
      const volBox = { x: padL, y: priceBox.y + priceBox.h + 8, w: priceBox.w, h: volH };
      const volumes = data.volume || [];
      const vRange = extent(volumes);
      if (vRange) {
        const barW = Math.max(1, volBox.w / Math.max(1, volumes.length) - 0.4);
        for (let i = 0; i < volumes.length; i++) {
          const v = volumes[i];
          if (v === null || v === undefined) continue;
          const x = volBox.x + (volBox.w * i) / Math.max(1, volumes.length - 1);
          const barH = (v / vRange[1]) * volBox.h;
          const rising = i > 0 && closes[i] !== null && closes[i - 1] !== null && closes[i] >= closes[i - 1];
          ctx.fillStyle = (rising ? THEME.up : THEME.down) + "66";
          ctx.fillRect(x - barW / 2, volBox.y + volBox.h - barH, barW, barH);
        }
      }
    }
    drawDateAxis(ctx, priceBox, data.dates || []);
  }

  /* ---------------- Indicator pane (RSI / MACD) ------------------------- */
  function indicatorChart(canvas, data, options) {
    options = options || {};
    const height = options.height || 110;
    const { ctx, w, h } = dpi(canvas, height);
    ctx.clearRect(0, 0, w, h);

    const series = options.series || [];
    let pool = [];
    series.forEach((s) => { pool = pool.concat(s.values || []); });
    (options.bands || []).forEach((b) => pool.push(b));
    const range = extent(pool);
    if (!range) {
      ctx.fillStyle = THEME.axis;
      ctx.font = THEME.mono;
      ctx.textAlign = "center";
      ctx.fillText("No data", w / 2, h / 2);
      return;
    }

    const padL = 52, padR = 10, padT = 6, padB = 6;
    const box = { x: padL, y: padT, w: w - padL - padR, h: height - padT - padB };
    const yMin = options.fixedMin !== undefined ? options.fixedMin : range[0];
    const yMax = options.fixedMax !== undefined ? options.fixedMax : range[1];

    drawGrid(ctx, box, yMin, yMax, 2);
    (options.bands || []).forEach((b) => hline(ctx, box, b, yMin, yMax, THEME.axis, [3, 3]));

    series.forEach((s, i) => {
      if (s.type === "histogram") {
        const barW = Math.max(1, box.w / Math.max(1, s.values.length) - 0.3);
        for (let j = 0; j < s.values.length; j++) {
          const v = s.values[j];
          if (v === null || v === undefined) continue;
          const x = box.x + (box.w * j) / Math.max(1, s.values.length - 1);
          const zero = box.y + box.h - ((0 - yMin) / (yMax - yMin)) * box.h;
          const y = box.y + box.h - ((v - yMin) / (yMax - yMin)) * box.h;
          ctx.fillStyle = (v >= 0 ? THEME.up : THEME.down) + "88";
          ctx.fillRect(x - barW / 2, Math.min(y, zero), barW, Math.abs(y - zero) || 1);
        }
      } else {
        line(ctx, box, s.values, yMin, yMax, s.color || SERIES_COLORS[i % SERIES_COLORS.length], 1.3);
      }
    });
  }

  /* ---------------- Equity curve vs benchmark --------------------------- */
  function equityChart(canvas, data, options) {
    options = options || {};
    const height = options.height || 260;
    const { ctx, w, h } = dpi(canvas, height);
    ctx.clearRect(0, 0, w, h);

    const series = data.series || [];
    if (!series.length || !series[0].values.filter((v) => v !== null).length) {
      ctx.fillStyle = THEME.axis;
      ctx.font = THEME.mono;
      ctx.textAlign = "center";
      ctx.fillText(options.emptyText || "No data to plot", w / 2, h / 2);
      return;
    }

    // Rebase every series to 100 so portfolio and benchmark are comparable.
    const rebased = series.map((s) => {
      const base = s.values.find((v) => v !== null && v !== 0);
      return {
        label: s.label,
        color: s.color,
        values: base ? s.values.map((v) => (v === null ? null : (v / base) * 100)) : s.values,
      };
    });

    let pool = [];
    rebased.forEach((s) => { pool = pool.concat(s.values); });
    const range = extent(pool);
    if (!range) return;
    const pad = (range[1] - range[0]) * 0.08;
    const yMin = range[0] - pad, yMax = range[1] + pad;

    const padL = 52, padR = 10, padT = 8, padB = 20;
    const box = { x: padL, y: padT, w: w - padL - padR, h: height - padT - padB };

    drawGrid(ctx, box, yMin, yMax, 4);
    hline(ctx, box, 100, yMin, yMax, THEME.axis, [4, 4]);
    rebased.forEach((s, i) => {
      const color = s.color || SERIES_COLORS[i % SERIES_COLORS.length];
      if (i === 0) area(ctx, box, s.values, yMin, yMax, color);
      line(ctx, box, s.values, yMin, yMax, color, i === 0 ? 1.8 : 1.2);
    });
    drawDateAxis(ctx, box, data.dates || []);
  }

  /* ---------------- Horizontal bar chart (exposures) -------------------- */
  function barChart(canvas, rows, options) {
    options = options || {};
    const rowH = options.rowHeight || 22;
    const height = Math.max(60, rows.length * rowH + 14);
    const { ctx, w } = dpi(canvas, height);
    ctx.clearRect(0, 0, w, height);
    if (!rows.length) {
      ctx.fillStyle = THEME.axis;
      ctx.font = THEME.mono;
      ctx.textAlign = "center";
      ctx.fillText(options.emptyText || "No exposures", w / 2, height / 2);
      return;
    }

    const labelW = options.labelWidth || 130;
    const valueW = 52;
    const barW = w - labelW - valueW - 8;
    const max = Math.max(options.limit || 0, ...rows.map((r) => Math.abs(r.value))) || 1;

    rows.forEach((row, i) => {
      const y = 7 + i * rowH;
      ctx.fillStyle = THEME.text;
      ctx.font = '11.5px system-ui, sans-serif';
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      const label = row.label.length > 20 ? row.label.slice(0, 19) + "…" : row.label;
      ctx.fillText(label, 0, y + rowH / 2 - 3);

      const width = (Math.abs(row.value) / max) * barW;
      const over = options.limit && Math.abs(row.value) > options.limit;
      ctx.fillStyle = row.color || (over ? THEME.down : THEME.accent);
      ctx.fillRect(labelW, y + 4, width, rowH - 12);

      ctx.fillStyle = over ? THEME.down : THEME.text;
      ctx.font = THEME.mono;
      ctx.textAlign = "right";
      ctx.fillText(options.format ? options.format(row.value) : fmtNum(row.value), w, y + rowH / 2 - 3);
    });

    if (options.limit) {
      const x = labelW + (options.limit / max) * barW;
      ctx.save();
      ctx.strokeStyle = THEME.down;
      ctx.setLineDash([3, 3]);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, 4);
      ctx.lineTo(x, height - 6);
      ctx.stroke();
      ctx.restore();
    }
  }

  const api = { priceChart, indicatorChart, equityChart, barChart, THEME, fmtNum };
  global.EGXCharts = api;

  // Redraw on resize so the terminal stays sharp at any width.
  let resizeTimer = null;
  global.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      if (typeof global.redrawCharts === "function") global.redrawCharts();
    }, 140);
  });
})(window);
