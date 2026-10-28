# Vendored browser libraries

The dashboard is local-first: it loads no script from a CDN at runtime. The knowledge
review page (`/knowledge/review`) draws with these, served by `/vendor/<file>` in
`src/jarvis/ui/app.py`. Every one is MIT; the full license texts are in `LICENSES.txt`.

| File | Package | Version | License | Source |
|---|---|---|---|---|
| `cytoscape.min.js` | cytoscape | 3.30.2 | MIT | https://cdn.jsdelivr.net/npm/cytoscape@3.30.2/dist/cytoscape.min.js |
| `layout-base.js` | layout-base | 2.0.1 | MIT | https://cdn.jsdelivr.net/npm/layout-base@2.0.1/layout-base.js |
| `cose-base.js` | cose-base | 2.2.0 | MIT | https://cdn.jsdelivr.net/npm/cose-base@2.2.0/cose-base.js |
| `cytoscape-fcose.js` | cytoscape-fcose | 2.2.0 | MIT | https://cdn.jsdelivr.net/npm/cytoscape-fcose@2.2.0/cytoscape-fcose.js |
| `cytoscape-edgehandles.js` | cytoscape-edgehandles | 4.0.1 | MIT | https://cdn.jsdelivr.net/npm/cytoscape-edgehandles@4.0.1/cytoscape-edgehandles.js |

Load order matters: `layout-base` → `cose-base` → `cytoscape-fcose` (each UMD build
reads the previous one off `window`). `cytoscape-edgehandles` expects lodash's
`memoize` and `throttle` on `window._`; the page defines a two-function shim instead
of vendoring lodash.

Drag-and-drop into domains is the page's own code, not `cytoscape-compound-drag-and-drop`:
that extension refuses to drag a compound node, a multi-selection, or to drop onto a
nested compound — the three gestures this page is for.

To upgrade: download the new file over the old one, update the table, and re-run
`scripts/screenshot_knowledge_review.py`.

sha256 (as vendored):

```
83e8c54a6bec655bfd81df07df605649c268af69aeca67a5ea2da54ea42dac81  cytoscape.min.js
ec15ab5df9af3f20708f4faab994accf91cda71848cd5bb10a23432cc50b6745  layout-base.js
7cae9509bd36235a63a85e71c8d9fa2cd0bc1d0c1ecc5b5a737976f39d040ddf  cose-base.js
4b1cab218d74996aa59cd8473f9239cc6398b8c1774d84d7e59ad9a68959cb57  cytoscape-fcose.js
0f3cc29c8d7a0306ff7895fc34483e7a18d2047e4ce152782dc94f2da83a16ee  cytoscape-edgehandles.js
```
