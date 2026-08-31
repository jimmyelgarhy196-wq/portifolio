/* GMG shell behaviour: mobile navigation, live ticker search, small helpers.
   No analytics, no third-party scripts, no external requests. */
(function () {
  "use strict";

  // ------------------------------------------------------------ mobile nav
  var toggle = document.getElementById("navToggle");
  var sidebar = document.getElementById("appSidebar");
  var scrim = document.getElementById("navScrim");
  function closeNav() {
    if (sidebar) sidebar.classList.remove("open");
    if (scrim) scrim.classList.remove("open");
  }
  if (toggle && sidebar) {
    toggle.addEventListener("click", function () {
      sidebar.classList.toggle("open");
      if (scrim) scrim.classList.toggle("open", sidebar.classList.contains("open"));
    });
  }
  if (scrim) scrim.addEventListener("click", closeNav);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeNav(); });

  // --------------------------------------------------------------- search
  var input = document.getElementById("gmgSearch");
  var panel = document.getElementById("gmgSearchResults");
  if (input && panel) {
    var timer = null;
    var active = -1;
    var hits = [];

    function close() { panel.classList.remove("open"); active = -1; }

    function render(rows) {
      hits = rows;
      if (!rows.length) {
        panel.innerHTML = '<div class="search-empty">No EGX company matches that search.</div>';
        panel.classList.add("open");
        return;
      }
      panel.innerHTML = rows.map(function (r, i) {
        var idx = r.indices && r.indices.length ? r.indices.join(" · ") : "";
        return '<a class="search-hit" data-i="' + i + '" href="/stock/' + encodeURIComponent(r.ticker) + '">' +
          '<span class="tk">' + r.ticker + '</span>' +
          '<span class="nm">' + (r.name || "") + '</span>' +
          '<span class="sc">' + (r.sector || "") + (idx ? " · " + idx : "") + '</span></a>';
      }).join("");
      panel.classList.add("open");
    }

    function search() {
      var q = input.value.trim();
      if (q.length < 1) { close(); return; }
      fetch("/api/search?q=" + encodeURIComponent(q))
        .then(function (r) { return r.ok ? r.json() : { results: [] }; })
        .then(function (d) { render(d.results || []); })
        .catch(function () { close(); });
    }

    input.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(search, 160);
    });
    input.addEventListener("focus", function () { if (input.value.trim()) search(); });
    input.addEventListener("keydown", function (e) {
      var items = panel.querySelectorAll(".search-hit");
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!items.length) return;
        active += e.key === "ArrowDown" ? 1 : -1;
        if (active < 0) active = items.length - 1;
        if (active >= items.length) active = 0;
        items.forEach(function (el, i) { el.classList.toggle("active", i === active); });
      } else if (e.key === "Enter") {
        if (active >= 0 && items[active]) { e.preventDefault(); items[active].click(); }
        else if (hits.length) { e.preventDefault(); window.location = "/stock/" + encodeURIComponent(hits[0].ticker); }
      } else if (e.key === "Escape") {
        close();
      }
    });
    document.addEventListener("click", function (e) {
      if (!panel.contains(e.target) && e.target !== input) close();
    });

    // "/" focuses search, the way a terminal does.
    document.addEventListener("keydown", function (e) {
      if (e.key === "/" && document.activeElement !== input &&
          !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {
        e.preventDefault();
        input.focus();
      }
    });
  }

  // ---------------------------------------------------------- sparklines
  function drawSparks() {
    if (!window.GMGChart) return;
    document.querySelectorAll("canvas[data-spark]").forEach(function (c) {
      var raw = c.getAttribute("data-spark");
      if (!raw) return;
      try {
        var values = JSON.parse(raw);
        if (values && values.length > 1) window.GMGChart.sparkline(c, values);
      } catch (err) { /* malformed series: leave the canvas blank rather than guess */ }
    });
    document.querySelectorAll("canvas[data-score]").forEach(function (c) {
      var v = c.getAttribute("data-score");
      window.GMGChart.scoreRing(c, v === "" || v === null ? null : parseFloat(v));
    });
  }
  drawSparks();
  var rt;
  window.addEventListener("resize", function () {
    clearTimeout(rt);
    rt = setTimeout(drawSparks, 160);
  });

  // --------------------------------------------------------- confirm forms
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  // ------------------------------------------------------------ tab memory
  document.querySelectorAll("[data-tabgroup]").forEach(function (group) {
    var key = group.getAttribute("data-tabgroup");
    group.querySelectorAll("[data-tab]").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.preventDefault();
        var name = btn.getAttribute("data-tab");
        group.querySelectorAll("[data-tab]").forEach(function (b) { b.classList.toggle("active", b === btn); });
        document.querySelectorAll('[data-tabpanel][data-group="' + key + '"]').forEach(function (p) {
          p.style.display = p.getAttribute("data-tabpanel") === name ? "" : "none";
        });
        try { sessionStorage.setItem("gmg-tab-" + key, name); } catch (err) {}
        if (window.__gmgTabChange) window.__gmgTabChange(name);
      });
    });
    var stored = null;
    try { stored = sessionStorage.getItem("gmg-tab-" + key); } catch (err) {}
    if (stored) {
      var target = group.querySelector('[data-tab="' + stored + '"]');
      if (target) target.click();
    }
  });
})();
