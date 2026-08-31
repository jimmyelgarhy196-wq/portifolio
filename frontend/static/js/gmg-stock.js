/* Stock page: loads price history for the chart and re-runs the DCF server-side. */
(function () {
  "use strict";

  var root = document.getElementById("gmgChart");
  if (root && window.GMGChart) {
    var chart = new window.GMGChart.PriceChart(root, { timeframe: "1Y", height: 360 });
    var empty = root.querySelector(".chart-empty");
    var sourceLine = document.getElementById("chartSource");
    if (empty) { empty.style.display = "block"; empty.textContent = "Loading price history…"; }

    fetch("/api/prices/" + encodeURIComponent(window.GMG_TICKER))
      .then(function (r) {
        if (!r.ok) throw new Error("prices unavailable");
        return r.json();
      })
      .then(function (d) {
        chart.setData(d.bars || []);
        if (sourceLine) {
          var label = (d.sources && d.sources.length)
            ? d.sources.join(", ")
            : "no source recorded";
          sourceLine.innerHTML =
            "Price history source: <strong>" + label + "</strong> · " +
            d.count + " daily bars · " + d.note +
            (d.is_demo
              ? ' <span class="badge badge-demo">Demo data</span>'
              : "");
        }
      })
      .catch(function () {
        chart.setData([]);
        if (empty) {
          empty.style.display = "block";
          empty.textContent = "N/A — price history could not be loaded.";
        }
        if (sourceLine) sourceLine.textContent = "Price history source: unavailable.";
      });
  }

  // ------------------------------------------------------------------ DCF
  var form = document.getElementById("dcfForm");
  if (form) {
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var payload = {};
      new FormData(form).forEach(function (v, k) { payload[k] = v; });
      var button = form.querySelector("button[type=submit]");
      if (button) { button.disabled = true; button.textContent = "Running…"; }

      fetch("/api/valuation/dcf", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      })
        .then(function (r) {
          if (r.status === 402 || r.status === 401) {
            throw new Error("A GMG subscription is required to run this model.");
          }
          if (!r.ok) throw new Error("The model could not be run with those assumptions.");
          return r.json();
        })
        .then(function (d) { renderDcf(d.result); })
        .catch(function (err) {
          var box = document.getElementById("dcfResult");
          if (box) {
            box.innerHTML = '<p class="flash error">' + err.message + "</p>";
          }
        })
        .finally(function () {
          if (button) { button.disabled = false; button.textContent = "Re-run model"; }
        });
    });
  }

  function fmt(v, digits) {
    if (v === null || v === undefined || isNaN(v)) return "N/A";
    return Number(v).toLocaleString(undefined, {
      minimumFractionDigits: digits, maximumFractionDigits: digits
    });
  }
  function pct(v, digits) {
    if (v === null || v === undefined || isNaN(v)) return "N/A";
    return (v >= 0 ? "+" : "") + (v * 100).toFixed(digits) + "%";
  }
  function compact(v) {
    if (v === null || v === undefined || isNaN(v)) return "N/A";
    var a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return (v / 1e6).toFixed(2) + "M";
    if (a >= 1e3) return (v / 1e3).toFixed(1) + "K";
    return v.toFixed(0);
  }

  function renderDcf(result) {
    var box = document.getElementById("dcfResult");
    if (!box) return;
    if (!result.available) {
      box.innerHTML = '<p class="flash warn">' + result.unavailable_reason + "</p>";
      return;
    }
    var dirClass = result.upside === null ? "" : (result.upside >= 0 ? "up" : "down");
    var rows = result.projections.map(function (p) {
      return "<tr><td>Year " + p.year + '</td><td class="num">' + pct(p.growth, 2) +
        '</td><td class="num">' + compact(p.fcf) +
        '</td><td class="num">' + fmt(p.discount_factor, 3) +
        '</td><td class="num">' + compact(p.present_value) + "</td></tr>";
    }).join("");

    box.innerHTML =
      '<div class="tiles g4 mb-16">' +
        '<div class="tile"><div class="k">Fair value per share</div><div class="v">' +
          fmt(result.fair_value_per_share, 2) + "</div></div>" +
        '<div class="tile"><div class="k">Implied upside</div><div class="v ' + dirClass + '">' +
          pct(result.upside, 1) + "</div></div>" +
        '<div class="tile"><div class="k">Discount rate</div><div class="v sm">' +
          pct(result.assumptions.discount_rate, 2) + '</div><div class="s">' +
          result.assumptions.discount_rate_source + "</div></div>" +
        '<div class="tile"><div class="k">From terminal value</div><div class="v sm">' +
          pct(result.terminal_share_of_value, 0) + '</div><div class="s">of enterprise value</div></div>' +
      "</div>" +
      '<div class="tbl-scroll"><table class="tbl compact"><thead><tr><th>Year</th>' +
      '<th class="num">Growth</th><th class="num">Free cash flow</th>' +
      '<th class="num">Discount factor</th><th class="num">Present value</th></tr></thead><tbody>' +
      rows + "</tbody></table></div>" +
      result.notes.map(function (n) {
        return '<p class="text-xs faint mt-16 mb-0">' + n + "</p>";
      }).join("");
  }
})();
