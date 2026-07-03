/* Shared motion helpers for Jarvis promo scenes (brand/BRAND.md).
 * Every scene defines window.seek(t) — fully deterministic per t — and the
 * renderer screenshots frame by frame. No wall-clock animation anywhere. */

const clamp = (v, lo = 0, hi = 1) => Math.min(hi, Math.max(lo, v));
/* progress 0→1 between t0 and t0+dur */
const seg = (t, t0, dur) => clamp((t - t0) / dur);
/* brand arrival easing (ease-out-quint feel) */
const easeOut = (p) => 1 - Math.pow(1 - p, 5);
const easeInOut = (p) => p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2;
const lerp = (a, b, p) => a + (b - a) * p;

/* scene-edge fade through --bg: 0.4s in, 0.4s out (brand motion rule 5) */
function edgeFade(t, total) {
  const veil = document.getElementById('veil');
  if (!veil) return;
  const a = 1 - seg(t, 0, 0.4) + seg(t, total - 0.4, 0.4);
  veil.style.opacity = clamp(a);
}

/* typewriter: reveal n chars of el's data-text by progress p */
function typeInto(el, p) {
  const full = el.dataset.text || '';
  const n = Math.round(full.length * clamp(p));
  el.textContent = full.slice(0, n);
}

/* deterministic caret blink (1.1s period) */
function caretOn(t) { return (t % 1.1) < 0.62; }

/* position a pulse dot along an SVG path by progress p */
function pulseAlong(dot, path, p, glowAt = null) {
  const len = path.getTotalLength();
  const pt = path.getPointAtLength(clamp(p) * len);
  dot.setAttribute('cx', pt.x);
  dot.setAttribute('cy', pt.y);
  const vis = p > 0 && p < 1;
  dot.style.opacity = vis ? 1 : 0;
  if (glowAt !== null) path.style.stroke = vis ? 'rgba(86,200,232,0.55)' : 'var(--line)';
}
