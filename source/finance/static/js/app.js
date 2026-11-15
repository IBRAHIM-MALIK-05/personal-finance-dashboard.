/* Progressive enhancement only: every page works without JavaScript.
   1. Chart reader: arrow keys / pointer move a cursor over the SVG and the
      value is written to an aria-live region (keyboard + screen reader access).
   2. Confirm dialogs for destructive actions (data-confirm).
   3. Moves focus to the main heading after navigation so screen-reader users
      hear the new page title first. */
(function () {
  "use strict";

  function initChart(fig) {
    var raw = fig.getAttribute("data-points");
    var data;
    try { data = JSON.parse(raw); } catch (e) { return; }
    if (!data || !data.points || !data.points.length) return;
    var wrap = fig.querySelector(".chart-wrap");
    var svg = fig.querySelector("svg.chart");
    var id = fig.getAttribute("data-chart");
    var cursor = document.getElementById(id + "-cursor");
    var readout = document.getElementById(id + "-readout");
    if (!wrap || !svg || !cursor || !readout) return;
    var line = cursor.querySelector("line");
    var dot = cursor.querySelector("circle");
    var pts = data.points;
    // start at the 'as of' boundary: the last actual point
    var idx = 0;
    for (var i = 0; i < pts.length; i++) { if (pts[i].t.indexOf("Actual") !== -1) idx = i; }

    function show(i) {
      idx = Math.max(0, Math.min(pts.length - 1, i));
      var p = pts[idx];
      cursor.setAttribute("visibility", "visible");
      line.setAttribute("x1", p.x); line.setAttribute("x2", p.x);
      dot.setAttribute("cx", p.x); dot.setAttribute("cy", p.y);
      readout.textContent = p.t;
    }

    wrap.addEventListener("keydown", function (ev) {
      var k = ev.key, step = 0;
      if (k === "ArrowRight" || k === "ArrowUp") step = 1;
      else if (k === "ArrowLeft" || k === "ArrowDown") step = -1;
      else if (k === "PageUp") step = 7;
      else if (k === "PageDown") step = -7;
      else if (k === "Home") { ev.preventDefault(); show(0); return; }
      else if (k === "End") { ev.preventDefault(); show(pts.length - 1); return; }
      else return;
      // In right-to-left layouts the chart keeps time flowing left-to-right, so keys keep their meaning.
      ev.preventDefault();
      show(idx + step);
    });
    wrap.addEventListener("focus", function () { show(idx); });

    function fromPointer(ev) {
      var ctm = svg.getScreenCTM();
      if (!ctm) return;
      var pt = svg.createSVGPoint();
      pt.x = ev.clientX; pt.y = ev.clientY;
      // screen -> SVG user space (inverse of the current transformation matrix), minus the plot margin
      var local = pt.matrixTransform(ctm.inverse());
      var x = local.x - data.ml;
      var best = 0, bestD = Infinity;
      for (var j = 0; j < pts.length; j++) {
        var d = Math.abs(pts[j].x - x);
        if (d < bestD) { bestD = d; best = j; }
      }
      show(best);
    }
    svg.addEventListener("pointermove", fromPointer);
    svg.addEventListener("pointerdown", fromPointer);
  }

  function initConfirm() {
    document.addEventListener("submit", function (ev) {
      var form = ev.target;
      var msg = form.getAttribute && form.getAttribute("data-confirm");
      if (msg && !window.confirm(msg)) ev.preventDefault();
    }, true);
  }

  document.addEventListener("DOMContentLoaded", function () {
    var figs = document.querySelectorAll("figure[data-chart]");
    for (var i = 0; i < figs.length; i++) initChart(figs[i]);
    initConfirm();
    var err = document.querySelector(".error-summary");
    if (err) { err.setAttribute("tabindex", "-1"); err.focus(); }
  });
})();
