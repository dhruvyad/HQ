// Runs inside /float (make.mjs injects it): draws a part of the real page as SVG, from what the
// browser laid out and computed. Boxes keep their backgrounds, borders, radii and shadows; text
// keeps its font, size, weight, colour and exact position, line by line; inline SVG icons and
// rings are copied with their computed paint. What it leaves out on purpose: pseudo-elements,
// images, and anything not visible. vector.mjs turns these drawings into animations.
(() => {
  const px = (v) => parseFloat(v) || 0;
  const n2 = (v) => String(Math.round(v * 100) / 100);
  const esc = (t) => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const paint = (c) => {
    const m = /rgba?\(([^)]+)\)/.exec(c || '');
    if (!m) return null;
    const [r, g, b, a = 1] = m[1].split(/[\s,/]+/).filter(Boolean).map(Number);
    return a > 0 ? {rgb: `rgb(${r},${g},${b})`, a} : null;
  };
  const fill = (c, attr = 'fill') => `${attr}="${c.rgb}"${c.a < 1 ? ` ${attr}-opacity="${n2(c.a)}"` : ''}`;
  const radius = (cs, r) => {
    const v = cs.borderTopLeftRadius;
    const n = v.endsWith('%') ? (parseFloat(v) / 100) * Math.min(r.width, r.height) : px(v);
    return Math.min(n, r.width / 2, r.height / 2);
  };
  const family = (cs) => (/Instrument Serif/.test(cs.fontFamily) ? 'display' : /mono/i.test(cs.fontFamily) ? 'mono' : 'sans');
  const ascents = new Map();
  // Chrome lays a line out with its font's ascent rounded to a whole pixel.
  const ascent = (cs) => {
    const key = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
    if (!ascents.has(key)) {
      const ctx = document.createElement('canvas').getContext('2d');
      ctx.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
      ascents.set(key, Math.round(ctx.measureText('Hg').fontBoundingBoxAscent));
    }
    return ascents.get(key);
  };
  const ellipsisWidth = (cs) => {
    const ctx = document.createElement('canvas').getContext('2d');
    ctx.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
    return ctx.measureText('…').width;
  };

  window.__snap = (root, {exclude = [], dx = 0, dy = 0, shell = false} = {}) => {
    const defs = [], out = [], chars = {sans: new Set(), display: new Set(), mono: new Set()};
    let ids = 0;
    const id = (p) => `${p}${(window.__snapIds = (window.__snapIds || 0) + 1)}${ids++}`;
    const X = (v) => n2(v + dx), Y = (v) => n2(v + dy);
    const shadow = (cs, r, rad) => {
      const s = cs.boxShadow;
      if (!s || s === 'none') return '';
      return s.split(/,(?![^(]*\))/).reverse().map((one) => {
        const m = /(rgba?\([^)]+\))\s+(-?[\d.]+)px\s+(-?[\d.]+)px\s+([\d.]+)px(?:\s+(-?[\d.]+)px)?(\s+inset)?/.exec(one.trim());
        if (!m || m[6]) return '';
        const c = paint(m[1]);
        if (!c) return '';
        const [ox, oy, blur, spread] = [Number(m[2]), Number(m[3]), Number(m[4]), Number(m[5] || 0)];
        if (!blur) {
          // A ring: 0 0 0 2px of a colour, drawn as a wider shape behind the box.
          return `<rect x="${X(r.left - spread + ox)}" y="${Y(r.top - spread + oy)}" width="${n2(r.width + 2 * spread)}" height="${n2(r.height + 2 * spread)}" rx="${n2(rad + spread)}" ${fill(c)}/>`;
        }
        const f = id('sh'), sd = blur / 2, pad = blur * 1.6 + Math.abs(ox) + Math.abs(oy) + spread;
        defs.push(`<filter id="${f}" filterUnits="userSpaceOnUse" x="${X(r.left - pad)}" y="${Y(r.top - pad)}" width="${n2(r.width + 2 * pad)}" height="${n2(r.height + 2 * pad)}"><feGaussianBlur stdDeviation="${n2(sd)}"/></filter>`);
        return `<rect x="${X(r.left - spread + ox)}" y="${Y(r.top - spread + oy)}" width="${n2(r.width + 2 * spread)}" height="${n2(r.height + 2 * spread)}" rx="${n2(rad + spread)}" ${fill(c)} filter="url(#${f})"/>`;
      }).join('');
    };
    const background = (cs, r, rad) => {
      const parts = [];
      const bg = paint(cs.backgroundColor);
      if (bg) parts.push(`<rect x="${X(r.left)}" y="${Y(r.top)}" width="${n2(r.width)}" height="${n2(r.height)}" rx="${n2(rad)}" ${fill(bg)}/>`);
      // A vertical linear-gradient, such as the fade under a notice.
      const g = /linear-gradient\((?:to bottom,\s*)?(.+)\)$/.exec(cs.backgroundImage);
      if (g) {
        const stops = g[1].split(/,(?![^(]*\))/).map((s) => {
          const m = /(rgba?\([^)]+\))\s*([\d.]+px|[\d.]+%)?/.exec(s.trim());
          return m && {c: paint(m[1]) || {rgb: 'rgb(0,0,0)', a: 0}, at: m[2]};
        }).filter(Boolean);
        const gid = id('lg');
        defs.push(`<linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">${stops.map((s, i) => {
          const at = s.at ? (s.at.endsWith('%') ? parseFloat(s.at) / 100 : px(s.at) / r.height) : i / Math.max(1, stops.length - 1);
          return `<stop offset="${n2(Math.min(1, at))}" stop-color="${s.c.rgb}" stop-opacity="${n2(s.c.a)}"/>`;
        }).join('')}</linearGradient>`);
        parts.push(`<rect x="${X(r.left)}" y="${Y(r.top)}" width="${n2(r.width)}" height="${n2(r.height)}" rx="${n2(rad)}" fill="url(#${gid})"/>`);
      }
      return parts.join('');
    };
    const border = (cs, r, rad) => {
      const sides = ['Top', 'Right', 'Bottom', 'Left'].map((s) => ({w: px(cs[`border${s}Width`]), c: paint(cs[`border${s}Color`]), style: cs[`border${s}Style`]}));
      const all = sides.every((s) => s.w === sides[0].w && s.c && sides[0].c && s.c.rgb === sides[0].c.rgb && s.c.a === sides[0].c.a && s.style === sides[0].style);
      if (all && sides[0].w && sides[0].style !== 'none') {
        const w = sides[0].w, dash = sides[0].style === 'dashed' ? ` stroke-dasharray="${n2(w * 3)} ${n2(w * 2.4)}"` : '';
        return `<rect x="${X(r.left + w / 2)}" y="${Y(r.top + w / 2)}" width="${n2(r.width - w)}" height="${n2(r.height - w)}" rx="${n2(Math.max(0, rad - w / 2))}" fill="none" ${fill(sides[0].c, 'stroke')} stroke-width="${n2(w)}"${dash}/>`;
      }
      // One or two edges, such as a hairline between rows.
      return sides.map((s, i) => {
        if (!s.w || !s.c || s.style === 'none') return '';
        const [x, y, w, h] = [[r.left, r.top, r.width, s.w], [r.right - s.w, r.top, s.w, r.height], [r.left, r.bottom - s.w, r.width, s.w], [r.left, r.top, s.w, r.height]][i];
        return `<rect x="${X(x)}" y="${Y(y)}" width="${n2(w)}" height="${n2(h)}" ${fill(s.c)}/>`;
      }).join('');
    };
    const text = (node, cs, parent) => {
      const value = node.textContent;
      if (!value) return;
      const color = paint(cs.color);
      if (!color) return;
      const range = document.createRange(), runs = [];
      let run = null;
      const box = parent.getBoundingClientRect();
      const clipRight = cs.textOverflow === 'ellipsis' && parent.scrollWidth > parent.clientWidth + 0.5
        ? box.right - px(cs.paddingRight) - px(cs.borderRightWidth) : null;
      const room = clipRight === null ? null : clipRight - ellipsisWidth(cs);
      let cut = false;
      for (let i = 0; i < value.length; i += 1) {
        range.setStart(node, i);
        range.setEnd(node, i + 1);
        const r = [...range.getClientRects()].find((x) => x.width > 0 || x.height > 0);
        if (!r || r.width === 0) continue;
        if (room !== null && r.right > room + 0.5) {
          cut = true;
          break;
        }
        if (!run || Math.abs(r.top - run.top) > 2 || r.left < run.right - 1) {
          run = {top: r.top, left: r.left, right: r.right, text: ''};
          runs.push(run);
        }
        run.text += value[i];
        run.right = r.right;
      }
      if (cut && run) run.text = `${run.text.replace(/\s+$/, '')}…`;
      const f = family(cs), asc = ascent(cs);
      const font = f === 'display' ? 'HQ Serif' : f === 'mono' ? 'ui-monospace, SFMono-Regular, Menlo, monospace' : 'HQ Inter';
      const tabular = /tabular-nums/.test(cs.fontVariantNumeric) ? ' style="font-variant-numeric: tabular-nums"' : '';
      const spacing = cs.letterSpacing !== 'normal' && px(cs.letterSpacing) ? ` letter-spacing="${n2(px(cs.letterSpacing))}"` : '';
      for (const r of runs) {
        const t = r.text.replace(/\s+/g, ' ');
        if (!t.trim()) continue;
        [...t].forEach((ch) => chars[f].add(ch));
        out.push(`<text x="${X(r.left)}" y="${Y(r.top + asc)}" font-family="${font}" font-size="${n2(px(cs.fontSize))}" font-weight="${cs.fontWeight}"${spacing} ${fill(color)}${tabular} xml:space="preserve">${esc(t)}</text>`);
      }
    };
    const svg = (el) => {
      const r = el.getBoundingClientRect(), cs = getComputedStyle(el), clone = el.cloneNode(true);
      const src = [el, ...el.querySelectorAll('*')], dst = [clone, ...clone.querySelectorAll('*')];
      src.forEach((s, i) => {
        const c = getComputedStyle(s), d = dst[i];
        for (const p of ['fill', 'stroke', 'stroke-width', 'stroke-linecap', 'stroke-linejoin', 'stroke-dasharray', 'stroke-dashoffset', 'fill-opacity', 'stroke-opacity']) {
          const v = c.getPropertyValue(p);
          if (v) d.setAttribute(p, v);
        }
        if (c.opacity !== '1' && s !== el) d.setAttribute('opacity', c.opacity);
        ['class', 'style', 'aria-hidden', 'focusable', 'role'].forEach((a) => d.removeAttribute(a));
      });
      const w = el.offsetWidth || r.width, h = el.offsetHeight || r.height, cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      clone.setAttribute('x', X(cx - w / 2));
      clone.setAttribute('y', Y(cy - h / 2));
      clone.setAttribute('width', n2(w));
      clone.setAttribute('height', n2(h));
      clone.setAttribute('overflow', 'visible');
      clone.removeAttribute('xmlns');
      const m = /matrix\(([^)]+)\)/.exec(cs.transform);
      const angle = m ? Math.round(Math.atan2(Number(m[1].split(',')[1]), Number(m[1].split(',')[0])) * 180 / Math.PI * 100) / 100 : 0;
      const html = clone.outerHTML.replace(/ xmlns="[^"]*"/g, '');
      out.push(angle ? `<g transform="rotate(${angle} ${X(cx)} ${Y(cy)})">${html}</g>` : html);
    };
    const visit = (el, top = false) => {
      if (exclude.some((sel) => el.matches(sel))) return;
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity) === 0) return;
      if (el.matches('.u-sr')) return;
      const r = el.getBoundingClientRect();
      if (!r.width && !r.height && cs.overflow === 'visible' && !el.childElementCount) return;
      const op = Number(cs.opacity);
      if (op < 1) out.push(`<g opacity="${n2(op)}">`);
      if (el instanceof SVGSVGElement) {
        svg(el);
      } else {
        const rad = radius(cs, r);
        if (!(top && shell)) out.push(shadow(cs, r, rad), background(cs, r, rad), border(cs, r, rad));
        const clips = !top && cs.overflow !== 'visible' && el.childNodes.length;
        if (clips) {
          const c = id('cl'), bw = px(cs.borderTopWidth);
          defs.push(`<clipPath id="${c}"><rect x="${X(r.left + bw)}" y="${Y(r.top + bw)}" width="${n2(r.width - 2 * bw)}" height="${n2(r.height - 2 * bw)}" rx="${n2(Math.max(0, rad - bw))}"/></clipPath>`);
          out.push(`<g clip-path="url(#${c})">`);
        }
        // Positioned children with a z-index paint after the rest, as they do in the page.
        const kids = [...el.childNodes], late = kids.filter((k) => k.nodeType === 1 && getComputedStyle(k).position !== 'static' && Number(getComputedStyle(k).zIndex) > 0);
        for (const k of kids) {
          if (late.includes(k)) continue;
          if (k.nodeType === 3) text(k, cs, el);
          else if (k.nodeType === 1) visit(k);
        }
        late.sort((a, b) => Number(getComputedStyle(a).zIndex) - Number(getComputedStyle(b).zIndex)).forEach((k) => visit(k));
        if (clips) out.push('</g>');
      }
      if (op < 1) out.push('</g>');
    };
    const el = typeof root === 'string' ? document.querySelector(root) : root;
    if (!el) return null;
    visit(el, true);
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return {
      svg: out.join(''), defs: defs.join(''),
      chars: Object.fromEntries(Object.entries(chars).map(([k, v]) => [k, [...v].join('')])),
      rect: {x: r.left + dx, y: r.top + dy, width: r.width, height: r.height},
      shell: {radius: radius(cs, r), background: cs.backgroundColor, border: cs.borderTopColor, borderWidth: px(cs.borderTopWidth), shadow: cs.boxShadow},
    };
  };
})();
