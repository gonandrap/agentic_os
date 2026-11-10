// The dashboard's own SVG chart pieces, no dependency and no CDN: every chart names its
// series INSIDE the drawing, so series identity never drifts into a prose caption (io-2641fe05).
// window.JarvisChart = { el, line, legend, pie }
(function (global) {
  var NS = 'http://www.w3.org/2000/svg';
  var FONT = { 'font-size': 11, 'font-family': 'var(--mono)' };

  function el(name, attrs, parent) {
    var e = document.createElementNS(NS, name);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }

  // One series: `pts` is [[x, y], ...] in pixels; `attrs` styles the polyline. null when empty.
  function line(parent, pts, attrs) {
    if (!pts.length) return null;
    return el('polyline', Object.assign({
      points: pts.map(function (p) { return p[0].toFixed(1) + ',' + p[1].toFixed(1); }).join(' '),
      fill: 'none' }, attrs), parent);
  }

  function swatch(parent, it) {
    if (it.kind === 'area') {
      return el('rect', { x: 0, y: -5, width: 14, height: 10, fill: it.color,
                          opacity: it.opacity == null ? 1 : it.opacity }, parent);
    }
    return el('line', { x1: 0, x2: 14, y1: 0, y2: 0, stroke: it.color, 'stroke-width': 2,
                        'stroke-dasharray': it.dash || '' }, parent);
  }

  // Items {key, label, color, kind: 'line'|'area', dash, opacity}, laid out in rows from
  // (opts.x, opts.y) and wrapped at opts.width, or one per row with opts.vertical.
  // Returns the height used, so the caller can place its plot below it.
  function legend(parent, items, opts) {
    var g = el('g', { 'class': 'legend' }, parent), ROW = 16;
    var x = 0, row = 0;
    items.forEach(function (it) {
      var item = el('g', { 'class': 'legend-item', 'data-key': it.key || '' }, g);
      swatch(item, it);
      var t = el('text', Object.assign({ x: 18, y: 0, fill: 'var(--ink-2)',
                                         'dominant-baseline': 'middle' }, FONT), item);
      t.textContent = it.label;
      var w = 18 + t.getComputedTextLength() + 14;
      if (opts.vertical) { x = 0; if (item.previousSibling) row++; }
      else if (x > 0 && x + w > opts.width) { x = 0; row++; }
      item.setAttribute('transform', 'translate(' + (opts.x + x) + ',' + (opts.y + row * ROW) + ')');
      x += w;
    });
    return items.length ? (row + 1) * ROW : 0;
  }

  // Slices {key, label, value, color} as a pie at (cx, cy, r), its legend to the right
  // with each slice's share. Zero slices are left out of both.
  function pie(parent, slices, opts) {
    var live = slices.filter(function (s) { return s.value > 0; });
    var total = live.reduce(function (a, s) { return a + s.value; }, 0);
    var g = el('g', { 'class': 'pie' }, parent), a = -Math.PI / 2;
    live.forEach(function (s) {
      var attrs = { fill: s.color, 'data-key': s.key || '' };
      if (s.value === total) {
        el('circle', Object.assign({ cx: opts.cx, cy: opts.cy, r: opts.r }, attrs), g);
        return;
      }
      var b = a + 2 * Math.PI * s.value / total;
      var p = function (t) {
        return (opts.cx + opts.r * Math.cos(t)).toFixed(2) + ' ' +
               (opts.cy + opts.r * Math.sin(t)).toFixed(2);
      };
      el('path', Object.assign({ d: 'M' + opts.cx + ' ' + opts.cy + ' L' + p(a) + ' A' + opts.r +
        ' ' + opts.r + ' 0 ' + (b - a > Math.PI ? 1 : 0) + ' 1 ' + p(b) + ' Z' }, attrs), g);
      a = b;
    });
    legend(parent, live.map(function (s) {
      return { key: s.key, color: s.color, kind: 'area',
               label: s.label + ' ' + Math.round(100 * s.value / total) + '%' };
    }), { x: opts.cx + opts.r + 16, y: opts.cy - opts.r + 6, vertical: true });
    return g;
  }

  global.JarvisChart = { el: el, line: line, legend: legend, pie: pie };
})(window);
