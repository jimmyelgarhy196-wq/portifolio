/* ==========================================================================
   GMG professional price chart
   Candlesticks, volume, overlays and oscillators on a plain 2D canvas.

   No charting library and no external requests: the page ships the data the
   server already rendered, so a chart cannot silently pull prices from
   somewhere unvetted. Every series drawn here comes from /api/prices, which
   carries its own source and freshness labelling.
   ========================================================================== */
(function (global) {
  "use strict";

  var CSSV = getComputedStyle(document.documentElement);
  function tok(name, fallback) {
    var v = CSSV.getPropertyValue(name);
    return (v && v.trim()) || fallback;
  }

  var T = {
    up: tok("--up", "#21c77a"),
    down: tok("--down", "#f0524d"),
    upFill: "rgba(33,199,122,.85)",
    downFill: "rgba(240,82,77,.85)",
    grid: "rgba(42,53,71,.55)",
    axis: tok("--text-faint", "#5f6d81"),
    text: tok("--text-dim", "#91a0b5"),
    bright: tok("--text-bright", "#ffffff"),
    gold: tok("--gold", "#d4af37"),
    accent: tok("--accent", "#4c9fff"),
    bg: tok("--bg-deep", "#05070b"),
    overlays: ["#4c9fff", "#d4af37", "#c77dff", "#21c77a", "#ff9f4c"]
  };

  // ---------------------------------------------------------------- helpers
  function dpi(canvas, cssW, cssH) {
    var ratio = global.devicePixelRatio || 1;
    canvas.width = Math.floor(cssW * ratio);
    canvas.height = Math.floor(cssH * ratio);
    canvas.style.height = cssH + "px";
    var ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    return ctx;
  }

  function nice(v, digits) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    var a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return (v / 1e6).toFixed(2) + "M";
    if (a >= 1e3 && digits === 0) return (v / 1e3).toFixed(1) + "K";
    return v.toFixed(digits === undefined ? (a < 1 ? 3 : 2) : digits);
  }

  function extent(values) {
    var lo = Infinity, hi = -Infinity;
    for (var i = 0; i < values.length; i++) {
      var v = values[i];
      if (v === null || v === undefined || isNaN(v)) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    if (lo === Infinity) return null;
    if (lo === hi) { lo -= Math.abs(lo || 1) * 0.05; hi += Math.abs(hi || 1) * 0.05; }
    return [lo, hi];
  }

  // ------------------------------------------------------------ indicators
  function sma(values, period) {
    var out = [], sum = 0, count = 0, queue = [];
    for (var i = 0; i < values.length; i++) {
      var v = values[i];
      queue.push(v);
      if (v !== null) { sum += v; count++; }
      if (queue.length > period) {
        var gone = queue.shift();
        if (gone !== null) { sum -= gone; count--; }
      }
      out.push(queue.length === period && count === period ? sum / period : null);
    }
    return out;
  }

  function ema(values, period) {
    var out = [], k = 2 / (period + 1), prev = null, seed = [], i;
    for (i = 0; i < values.length; i++) {
      var v = values[i];
      if (v === null) { out.push(prev); continue; }
      if (prev === null) {
        seed.push(v);
        if (seed.length < period) { out.push(null); continue; }
        prev = seed.reduce(function (a, b) { return a + b; }, 0) / period;
      } else {
        prev = v * k + prev * (1 - k);
      }
      out.push(prev);
    }
    return out;
  }

  function stddev(values, period) {
    var out = [];
    for (var i = 0; i < values.length; i++) {
      if (i < period - 1) { out.push(null); continue; }
      var slice = values.slice(i - period + 1, i + 1).filter(function (v) { return v !== null; });
      if (slice.length < period) { out.push(null); continue; }
      var m = slice.reduce(function (a, b) { return a + b; }, 0) / period;
      var varr = slice.reduce(function (a, b) { return a + (b - m) * (b - m); }, 0) / period;
      out.push(Math.sqrt(varr));
    }
    return out;
  }

  function rsi(values, period) {
    var out = [null], gains = [], losses = [];
    for (var i = 1; i < values.length; i++) {
      var a = values[i - 1], b = values[i];
      if (a === null || b === null) { gains.push(0); losses.push(0); out.push(null); continue; }
      var d = b - a;
      gains.push(Math.max(d, 0));
      losses.push(Math.max(-d, 0));
      if (gains.length < period) { out.push(null); continue; }
      var g = gains.slice(-period).reduce(function (x, y) { return x + y; }, 0) / period;
      var l = losses.slice(-period).reduce(function (x, y) { return x + y; }, 0) / period;
      out.push(l === 0 ? 100 : 100 - 100 / (1 + g / l));
    }
    return out;
  }

  function macd(values, fast, slow, signal) {
    var f = ema(values, fast), s = ema(values, slow);
    var line = values.map(function (_, i) {
      return (f[i] === null || s[i] === null) ? null : f[i] - s[i];
    });
    var sig = ema(line.map(function (v) { return v === null ? null : v; }), signal);
    var hist = line.map(function (v, i) {
      return (v === null || sig[i] === null) ? null : v - sig[i];
    });
    return { line: line, signal: sig, hist: hist };
  }

  global.GMGIndicators = { sma: sma, ema: ema, rsi: rsi, macd: macd, stddev: stddev };

  // ----------------------------------------------------------------- chart
  function PriceChart(root, options) {
    this.root = root;
    this.opts = options || {};
    this.bars = [];
    this.timeframe = this.opts.timeframe || "1Y";
    this.chartType = this.opts.chartType || "candle";
    this.overlays = { sma20: false, sma50: true, sma200: true, bb: false };
    this.pane = null;           // "rsi" | "macd" | null
    this.hover = null;
    this.canvas = root.querySelector("canvas.price");
    this.volCanvas = root.querySelector("canvas.volume");
    this.paneCanvas = root.querySelector("canvas.pane");
    this.readout = root.querySelector(".chart-readout");
    this._bind();
  }

  var TIMEFRAMES = {
    "1D": 2, "1W": 7, "1M": 23, "3M": 66, "6M": 130,
    "YTD": null, "1Y": 252, "5Y": 1260, "MAX": null
  };

  PriceChart.prototype.setData = function (bars) {
    this.bars = (bars || []).slice();
    this.render();
  };

  PriceChart.prototype.visibleBars = function () {
    var bars = this.bars;
    if (!bars.length) return [];
    if (this.timeframe === "MAX") return bars;
    if (this.timeframe === "YTD") {
      var year = new Date(bars[bars.length - 1].date).getUTCFullYear();
      return bars.filter(function (b) { return new Date(b.date).getUTCFullYear() === year; });
    }
    var n = TIMEFRAMES[this.timeframe] || 252;
    return bars.slice(Math.max(0, bars.length - n));
  };

  PriceChart.prototype._bind = function () {
    var self = this;
    this.root.querySelectorAll(".tf-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        self.root.querySelectorAll(".tf-btn").forEach(function (b) { b.classList.remove("active"); });
        btn.classList.add("active");
        self.timeframe = btn.dataset.tf;
        self.render();
      });
    });
    this.root.querySelectorAll(".ind-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var key = btn.dataset.ind;
        if (key === "rsi" || key === "macd") {
          self.pane = self.pane === key ? null : key;
          self.root.querySelectorAll('.ind-btn[data-ind="rsi"],.ind-btn[data-ind="macd"]')
            .forEach(function (b) { b.classList.remove("active"); });
          if (self.pane) btn.classList.add("active");
        } else if (key === "candle" || key === "line" || key === "area") {
          self.chartType = key;
          self.root.querySelectorAll('.ind-btn[data-ind="candle"],.ind-btn[data-ind="line"],.ind-btn[data-ind="area"]')
            .forEach(function (b) { b.classList.remove("active"); });
          btn.classList.add("active");
        } else {
          self.overlays[key] = !self.overlays[key];
          btn.classList.toggle("active", self.overlays[key]);
        }
        self.render();
      });
    });

    if (this.canvas) {
      this.canvas.addEventListener("mousemove", function (e) {
        var rect = self.canvas.getBoundingClientRect();
        self.hover = { x: e.clientX - rect.left };
        self.render();
      });
      this.canvas.addEventListener("mouseleave", function () {
        self.hover = null;
        self.render();
      });
    }

    var timer;
    global.addEventListener("resize", function () {
      clearTimeout(timer);
      timer = setTimeout(function () { self.render(); }, 140);
    });
  };

  PriceChart.prototype.render = function () {
    var bars = this.visibleBars();
    var empty = this.root.querySelector(".chart-empty");
    var wrap = this.root.querySelector(".chart-canvas-wrap");
    if (!bars.length) {
      if (wrap) wrap.style.display = "none";
      if (empty) { empty.style.display = "block"; empty.textContent = "N/A — no price history available for this timeframe."; }
      return;
    }
    if (wrap) wrap.style.display = "";
    if (empty) empty.style.display = "none";

    var closes = bars.map(function (b) { return b.close; });
    this._drawPrice(bars, closes);
    this._drawVolume(bars);
    this._drawPane(bars, closes);
    this._readout(bars, closes);
  };

  PriceChart.prototype._geometry = function (canvas, height) {
    var cssW = canvas.parentNode.clientWidth || canvas.clientWidth || 640;
    var ctx = dpi(canvas, cssW, height);
    ctx.clearRect(0, 0, cssW, height);
    return { ctx: ctx, w: cssW, h: height, left: 8, right: 62, top: 10, bottom: 8 };
  };

  PriceChart.prototype._grid = function (g, lo, hi, digits) {
    var ctx = g.ctx;
    var plotH = g.h - g.top - g.bottom;
    ctx.font = "10px ui-monospace, monospace";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    for (var i = 0; i <= 4; i++) {
      var y = g.top + (plotH * i) / 4;
      var value = hi - ((hi - lo) * i) / 4;
      ctx.strokeStyle = T.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(g.left, Math.round(y) + 0.5);
      ctx.lineTo(g.w - g.right, Math.round(y) + 0.5);
      ctx.stroke();
      ctx.fillStyle = T.axis;
      ctx.fillText(nice(value, digits), g.w - g.right + 7, y);
    }
  };

  PriceChart.prototype._x = function (g, i, n) {
    var plotW = g.w - g.left - g.right;
    return g.left + (n <= 1 ? plotW / 2 : (plotW * i) / (n - 1));
  };

  PriceChart.prototype._drawPrice = function (bars, closes) {
    var g = this._geometry(this.canvas, this.opts.height || 340);
    var ctx = g.ctx;
    var n = bars.length;
    var pool = [];
    bars.forEach(function (b) {
      [b.high, b.low, b.close, b.open].forEach(function (v) { if (v !== null && v !== undefined) pool.push(v); });
    });

    var overlaySeries = [];
    if (this.overlays.sma20) overlaySeries.push({ label: "SMA 20", values: sma(closes, 20) });
    if (this.overlays.sma50) overlaySeries.push({ label: "SMA 50", values: sma(closes, 50) });
    if (this.overlays.sma200) overlaySeries.push({ label: "SMA 200", values: sma(closes, 200) });
    if (this.overlays.bb) {
      var mid = sma(closes, 20), sd = stddev(closes, 20);
      overlaySeries.push({ label: "BB upper", values: mid.map(function (m, i) { return m === null || sd[i] === null ? null : m + 2 * sd[i]; }), dash: [4, 3] });
      overlaySeries.push({ label: "BB lower", values: mid.map(function (m, i) { return m === null || sd[i] === null ? null : m - 2 * sd[i]; }), dash: [4, 3] });
    }
    overlaySeries.forEach(function (s) {
      s.values.forEach(function (v) { if (v !== null) pool.push(v); });
    });

    var ext = extent(pool);
    if (!ext) return;
    var pad = (ext[1] - ext[0]) * 0.06;
    var lo = ext[0] - pad, hi = ext[1] + pad;
    this._lo = lo; this._hi = hi; this._g = g;

    this._grid(g, lo, hi);
    this._dateAxis(g, bars);

    var plotH = g.h - g.top - g.bottom;
    var self = this;
    function yOf(v) { return g.top + plotH * (1 - (v - lo) / (hi - lo)); }
    this._yOf = yOf;

    if (this.chartType === "candle") {
      var plotW = g.w - g.left - g.right;
      var slot = plotW / Math.max(n, 1);
      var bw = Math.max(1, Math.min(11, slot * 0.68));
      bars.forEach(function (b, i) {
        if (b.close === null || b.close === undefined) return;
        var x = self._x(g, i, n);
        var o = (b.open === null || b.open === undefined) ? b.close : b.open;
        var h = (b.high === null || b.high === undefined) ? Math.max(o, b.close) : b.high;
        var l = (b.low === null || b.low === undefined) ? Math.min(o, b.close) : b.low;
        var rising = b.close >= o;
        ctx.strokeStyle = rising ? T.up : T.down;
        ctx.fillStyle = rising ? T.upFill : T.downFill;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(Math.round(x) + 0.5, yOf(h));
        ctx.lineTo(Math.round(x) + 0.5, yOf(l));
        ctx.stroke();
        var top = yOf(Math.max(o, b.close));
        var bot = yOf(Math.min(o, b.close));
        var bh = Math.max(1, bot - top);
        if (bw <= 1.6) {
          ctx.fillRect(Math.round(x), top, 1, bh);
        } else {
          ctx.fillRect(Math.round(x - bw / 2), top, Math.round(bw), bh);
        }
      });
    } else {
      var pts = [];
      bars.forEach(function (b, i) {
        if (b.close === null || b.close === undefined) return;
        pts.push([self._x(g, i, n), yOf(b.close)]);
      });
      if (pts.length) {
        var first = bars.find(function (b) { return b.close !== null; });
        var last = closes.slice().reverse().find(function (v) { return v !== null; });
        var rising = last >= (first ? first.close : last);
        var col = rising ? T.up : T.down;
        if (this.chartType === "area") {
          var grad = ctx.createLinearGradient(0, g.top, 0, g.h - g.bottom);
          grad.addColorStop(0, rising ? "rgba(33,199,122,.28)" : "rgba(240,82,77,.28)");
          grad.addColorStop(1, "rgba(0,0,0,0)");
          ctx.fillStyle = grad;
          ctx.beginPath();
          ctx.moveTo(pts[0][0], g.h - g.bottom);
          pts.forEach(function (p) { ctx.lineTo(p[0], p[1]); });
          ctx.lineTo(pts[pts.length - 1][0], g.h - g.bottom);
          ctx.closePath();
          ctx.fill();
        }
        ctx.strokeStyle = col;
        ctx.lineWidth = 1.6;
        ctx.beginPath();
        pts.forEach(function (p, i) { i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]); });
        ctx.stroke();
      }
    }

    overlaySeries.forEach(function (s, idx) {
      ctx.strokeStyle = T.overlays[idx % T.overlays.length];
      ctx.lineWidth = 1.2;
      ctx.setLineDash(s.dash || []);
      ctx.beginPath();
      var started = false;
      s.values.forEach(function (v, i) {
        if (v === null) { started = false; return; }
        var x = self._x(g, i, n), y = yOf(v);
        if (!started) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, y); }
      });
      ctx.stroke();
      ctx.setLineDash([]);
    });
    this._overlaySeries = overlaySeries;

    // last-price marker
    var lastClose = null, lastIdx = -1;
    for (var i = n - 1; i >= 0; i--) {
      if (bars[i].close !== null && bars[i].close !== undefined) { lastClose = bars[i].close; lastIdx = i; break; }
    }
    if (lastClose !== null) {
      var y = yOf(lastClose);
      ctx.strokeStyle = "rgba(212,175,55,.5)";
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(g.left, y);
      ctx.lineTo(g.w - g.right, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = T.gold;
      ctx.fillRect(g.w - g.right + 2, y - 8, g.right - 4, 16);
      ctx.fillStyle = "#10131a";
      ctx.font = "bold 10px ui-monospace, monospace";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(nice(lastClose), g.w - g.right + 2 + (g.right - 4) / 2, y);
    }

    this._crosshair(g, bars, n);
  };

  PriceChart.prototype._crosshair = function (g, bars, n) {
    if (!this.hover) { this._hoverIndex = null; return; }
    var plotW = g.w - g.left - g.right;
    var rel = (this.hover.x - g.left) / plotW;
    var idx = Math.round(rel * (n - 1));
    if (idx < 0 || idx >= n) { this._hoverIndex = null; return; }
    this._hoverIndex = idx;
    var ctx = g.ctx;
    var x = this._x(g, idx, n);
    ctx.strokeStyle = "rgba(145,160,181,.45)";
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(Math.round(x) + 0.5, g.top);
    ctx.lineTo(Math.round(x) + 0.5, g.h - g.bottom);
    ctx.stroke();
    ctx.setLineDash([]);
  };

  PriceChart.prototype._dateAxis = function (g, bars) {
    var ctx = g.ctx;
    var n = bars.length;
    var steps = Math.min(6, n);
    ctx.fillStyle = T.axis;
    ctx.font = "10px ui-monospace, monospace";
    ctx.textBaseline = "top";
    for (var i = 0; i < steps; i++) {
      var idx = Math.round((n - 1) * (i / Math.max(steps - 1, 1)));
      var x = this._x(g, idx, n);
      var d = bars[idx].date;
      ctx.textAlign = i === 0 ? "left" : (i === steps - 1 ? "right" : "center");
      ctx.fillText(String(d).slice(2, 10), x, g.h - g.bottom + 2);
    }
  };

  PriceChart.prototype._drawVolume = function (bars) {
    if (!this.volCanvas) return;
    var g = this._geometry(this.volCanvas, 74);
    var ctx = g.ctx;
    var n = bars.length;
    var vols = bars.map(function (b) { return b.volume; });
    var ext = extent(vols.filter(function (v) { return v !== null && v !== undefined; }));
    if (!ext) {
      ctx.fillStyle = T.axis;
      ctx.font = "11px ui-monospace, monospace";
      ctx.textAlign = "center";
      ctx.fillText("N/A — volume unavailable", g.w / 2, g.h / 2);
      return;
    }
    var hi = ext[1];
    var plotH = g.h - g.top - g.bottom;
    var plotW = g.w - g.left - g.right;
    var slot = plotW / Math.max(n, 1);
    var bw = Math.max(1, Math.min(11, slot * 0.68));
    var self = this;
    bars.forEach(function (b, i) {
      if (b.volume === null || b.volume === undefined) return;
      var x = self._x(g, i, n);
      var h = Math.max(1, (b.volume / hi) * plotH);
      var rising = b.close !== null && b.open !== null && b.close >= b.open;
      ctx.fillStyle = rising ? "rgba(33,199,122,.42)" : "rgba(240,82,77,.42)";
      ctx.fillRect(Math.round(x - bw / 2), g.h - g.bottom - h, Math.max(1, Math.round(bw)), h);
    });
    ctx.fillStyle = T.axis;
    ctx.font = "9.5px ui-monospace, monospace";
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.fillText("VOL  " + nice(hi, 0), g.left + 2, g.top);
  };

  PriceChart.prototype._drawPane = function (bars, closes) {
    if (!this.paneCanvas) return;
    if (!this.pane) { this.paneCanvas.style.display = "none"; return; }
    this.paneCanvas.style.display = "";
    var g = this._geometry(this.paneCanvas, 96);
    var ctx = g.ctx;
    var n = bars.length;
    var self = this;
    var plotH = g.h - g.top - g.bottom;

    if (this.pane === "rsi") {
      var values = rsi(closes, 14);
      var lo = 0, hi = 100;
      function y(v) { return g.top + plotH * (1 - (v - lo) / (hi - lo)); }
      [30, 50, 70].forEach(function (level) {
        ctx.strokeStyle = level === 50 ? T.grid : "rgba(240,180,41,.28)";
        ctx.setLineDash(level === 50 ? [] : [3, 3]);
        ctx.beginPath();
        ctx.moveTo(g.left, y(level));
        ctx.lineTo(g.w - g.right, y(level));
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = T.axis;
        ctx.font = "9.5px ui-monospace, monospace";
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.fillText(String(level), g.w - g.right + 7, y(level));
      });
      ctx.strokeStyle = T.accent;
      ctx.lineWidth = 1.3;
      ctx.beginPath();
      var started = false;
      values.forEach(function (v, i) {
        if (v === null) { started = false; return; }
        var x = self._x(g, i, n);
        if (!started) { ctx.moveTo(x, y(v)); started = true; } else { ctx.lineTo(x, y(v)); }
      });
      ctx.stroke();
      ctx.fillStyle = T.text;
      ctx.font = "9.5px ui-monospace, monospace";
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.fillText("RSI (14)", g.left + 2, g.top);
      this._paneValues = { label: "RSI(14)", values: values };
    } else {
      var m = macd(closes, 12, 26, 9);
      var pool = m.line.concat(m.signal, m.hist).filter(function (v) { return v !== null; });
      var ext = extent(pool);
      if (!ext) return;
      var lo2 = ext[0], hi2 = ext[1];
      function y2(v) { return g.top + plotH * (1 - (v - lo2) / (hi2 - lo2)); }
      var plotW = g.w - g.left - g.right;
      var bw = Math.max(1, Math.min(6, (plotW / Math.max(n, 1)) * 0.62));
      m.hist.forEach(function (v, i) {
        if (v === null) return;
        var x = self._x(g, i, n);
        var zero = y2(0), yy = y2(v);
        ctx.fillStyle = v >= 0 ? "rgba(33,199,122,.5)" : "rgba(240,82,77,.5)";
        ctx.fillRect(Math.round(x - bw / 2), Math.min(zero, yy), Math.max(1, bw), Math.abs(zero - yy) || 1);
      });
      [[m.line, T.accent], [m.signal, T.gold]].forEach(function (pair) {
        ctx.strokeStyle = pair[1];
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        var st = false;
        pair[0].forEach(function (v, i) {
          if (v === null) { st = false; return; }
          var x = self._x(g, i, n);
          if (!st) { ctx.moveTo(x, y2(v)); st = true; } else { ctx.lineTo(x, y2(v)); }
        });
        ctx.stroke();
      });
      ctx.fillStyle = T.text;
      ctx.font = "9.5px ui-monospace, monospace";
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.fillText("MACD (12, 26, 9)", g.left + 2, g.top);
      this._paneValues = { label: "MACD", values: m.line };
    }
  };

  PriceChart.prototype._readout = function (bars, closes) {
    if (!this.readout) return;
    var idx = this._hoverIndex;
    if (idx === null || idx === undefined) idx = bars.length - 1;
    var b = bars[idx];
    if (!b) return;
    var prev = idx > 0 ? bars[idx - 1].close : null;
    var chg = (b.close !== null && prev !== null && prev) ? (b.close - prev) / prev : null;
    var cls = chg === null ? "" : (chg >= 0 ? "up" : "down");
    var parts = [
      '<div><b>' + String(b.date).slice(0, 10) + '</b></div>',
      '<div>O <b>' + nice(b.open) + '</b> H <b>' + nice(b.high) + '</b> ' +
      'L <b>' + nice(b.low) + '</b> C <b>' + nice(b.close) + '</b>' +
      (chg === null ? '' : ' <span class="' + cls + '">' + (chg >= 0 ? '+' : '') + (chg * 100).toFixed(2) + '%</span>') +
      '</div>',
      '<div>Vol <b>' + nice(b.volume, 0) + '</b></div>'
    ];
    (this._overlaySeries || []).forEach(function (s, i) {
      var v = s.values[idx];
      parts.push('<div style="color:' + T.overlays[i % T.overlays.length] + '">' +
        s.label + ' <b>' + (v === null || v === undefined ? "—" : nice(v)) + '</b></div>');
    });
    if (this.pane && this._paneValues) {
      var pv = this._paneValues.values[idx];
      parts.push('<div>' + this._paneValues.label + ' <b>' +
        (pv === null || pv === undefined ? "—" : nice(pv)) + '</b></div>');
    }
    this.readout.innerHTML = parts.join("");
  };

  // ------------------------------------------------------------- sparkline
  function sparkline(canvas, values, options) {
    options = options || {};
    if (!values || values.length < 2) return;
    var cssW = canvas.parentNode.clientWidth || 200;
    var h = options.height || 46;
    var ctx = dpi(canvas, cssW, h);
    ctx.clearRect(0, 0, cssW, h);
    var ext = extent(values);
    if (!ext) return;
    var lo = ext[0], hi = ext[1];
    var pad = 4;
    function y(v) { return pad + (h - pad * 2) * (1 - (v - lo) / (hi - lo)); }
    function x(i) { return (cssW * i) / (values.length - 1); }
    var rising = values[values.length - 1] >= values[0];
    var col = options.color || (rising ? T.up : T.down);
    var grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, rising ? "rgba(33,199,122,.28)" : "rgba(240,82,77,.28)");
    grad.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.moveTo(0, h);
    values.forEach(function (v, i) { ctx.lineTo(x(i), y(v)); });
    ctx.lineTo(cssW, h);
    ctx.closePath();
    ctx.fill();
    ctx.strokeStyle = col;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    values.forEach(function (v, i) { i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)); });
    ctx.stroke();
  }

  // ------------------------------------------------------------- score ring
  function scoreRing(canvas, value, options) {
    options = options || {};
    var size = options.size || 132;
    var ctx = dpi(canvas, size, size);
    ctx.clearRect(0, 0, size, size);
    var cx = size / 2, cy = size / 2, r = size / 2 - 10;
    ctx.lineWidth = 9;
    ctx.strokeStyle = "rgba(42,53,71,.7)";
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.stroke();
    if (value === null || value === undefined || isNaN(value)) return;
    var pct = Math.max(0, Math.min(100, value)) / 100;
    var col = value >= 75 ? T.up : (value >= 60 ? T.accent : (value >= 45 ? "#f0b429" : T.down));
    ctx.strokeStyle = col;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.arc(cx, cy, r, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * pct);
    ctx.stroke();
  }

  global.GMGChart = {
    PriceChart: PriceChart,
    sparkline: sparkline,
    scoreRing: scoreRing,
    timeframes: Object.keys(TIMEFRAMES)
  };
})(window);
