/* Accessible chart reader (progressive enhancement: every chart also has a table).
   Arrow keys or the pointer move a cursor over the SVG cash-flow chart, and the
   value under it is written to an aria-live region for screen readers.

   Keys: Left/Right one day, PageUp/PageDown one week, Home/End first/last day. */
(function () {
  "use strict";

  function initChart(figure) {
    var data;
    try { data = JSON.parse(figure.getAttribute("data-points")); } catch (e) { return; }
    if (!data || !data.points || !data.points.length) return;

    var id = figure.getAttribute("data-chart");
    var wrap = figure.querySelector(".chart-wrap");
    var svg = figure.querySelector("svg.chart");
    var cursor = document.getElementById(id + "-cursor");
    var readout = document.getElementById(id + "-readout");
    if (!wrap || !svg || !cursor || !readout) return;

    var line = cursor.querySelector("line");
    var dot = cursor.querySelector("circle");
    var points = data.points;

    // Start at the "as of" boundary: the last actual point.
    var index = 0;
    points.forEach(function (p, i) { if (p.t.indexOf("Actual") !== -1) index = i; });

    function show(i) {
      index = Math.max(0, Math.min(points.length - 1, i));
      var p = points[index];
      cursor.setAttribute("visibility", "visible");
      line.setAttribute("x1", p.x);
      line.setAttribute("x2", p.x);
      dot.setAttribute("cx", p.x);
      dot.setAttribute("cy", p.y);
      readout.textContent = p.t;
    }

    var STEPS = {
      ArrowRight: 1,
      ArrowUp: 1,
      ArrowLeft: -1,
      ArrowDown: -1,
      PageUp: -7,
      PageDown: 7
    };
    wrap.addEventListener("keydown", function (ev) {
      if (ev.key === "Home") { ev.preventDefault(); show(0); return; }
      if (ev.key === "End") { ev.preventDefault(); show(points.length - 1); return; }
      if (!(ev.key in STEPS)) return;
      ev.preventDefault();
      show(index + STEPS[ev.key]);
    });
    wrap.addEventListener("focus", function () { show(index); });

    function fromPointer(ev) {
      var ctm = svg.getScreenCTM();
      if (!ctm) return;
      var pt = svg.createSVGPoint();
      pt.x = ev.clientX;
      pt.y = ev.clientY;
      // screen -> SVG user space (inverse of the current transformation matrix), minus the plot margin
      var x = pt.matrixTransform(ctm.inverse()).x - data.ml;
      var best = 0, bestDistance = Infinity;
      points.forEach(function (p, i) {
        var d = Math.abs(p.x - x);
        if (d < bestDistance) { bestDistance = d; best = i; }
      });
      show(best);
    }
    svg.addEventListener("pointermove", fromPointer);
    svg.addEventListener("pointerdown", fromPointer);
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("figure[data-chart]").forEach(initChart);
  });
})();
