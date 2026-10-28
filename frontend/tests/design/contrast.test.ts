// @vitest-environment node
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

/*
 * WCAG AA guard for the liquid-glass tokens in src/app/globals.css.
 *
 * Worst-case model of a text pixel: one blob at full strength (no blur
 * averaging) over each backdrop gradient stop, then the glass
 * `saturate(170%)` backdrop filter (applied in both sRGB and linear-light,
 * since engines differ), then --glass-bg on top, then optionally --chip-bg.
 * The solid fallback (--glass-solid, with and without a chip) is checked too.
 */

type Rgb = readonly [number, number, number];
interface Rgba {
  rgb: Rgb;
  alpha: number;
}

const css = readFileSync(
  resolve(import.meta.dirname, "../../src/app/globals.css"),
  "utf8",
);

function block(selector: string): string {
  const start = css.indexOf(`${selector} {`);
  if (start < 0) throw new Error(`missing block ${selector}`);
  const end = css.indexOf("}", start);
  return css.slice(start, end);
}

function tokens(selector: string): Map<string, string> {
  const map = new Map<string, string>();
  const body = block(selector);
  for (const match of body.matchAll(/(--[\w-]+):\s*([^;]+);/g)) {
    const [, name, value] = match;
    if (name && value) map.set(name, value.replace(/\s+/g, " ").trim());
  }
  return map;
}

function parseColor(value: string): Rgba {
  const hex = /^#([0-9a-f]{6})$/i.exec(value);
  if (hex?.[1]) {
    const h = hex[1];
    return {
      rgb: [0, 2, 4].map((i) => Number.parseInt(h.slice(i, i + 2), 16)) as [
        number,
        number,
        number,
      ],
      alpha: 1,
    };
  }
  const rgb = /^rgb\(\s*(\d+)\s+(\d+)\s+(\d+)\s*(?:\/\s*([\d.]+))?\s*\)/.exec(
    value,
  );
  if (rgb) {
    return {
      rgb: [Number(rgb[1]), Number(rgb[2]), Number(rgb[3])],
      alpha: rgb[4] === undefined ? 1 : Number(rgb[4]),
    };
  }
  throw new Error(`unparseable colour ${value}`);
}

function gradientStops(value: string): Rgb[] {
  return [...value.matchAll(/#[0-9a-f]{6}/gi)].map((m) => parseColor(m[0]).rgb);
}

function over(top: Rgba, bottom: Rgb): Rgb {
  return top.rgb.map(
    (c, i) => c * top.alpha + (bottom[i] ?? 0) * (1 - top.alpha),
  ) as unknown as Rgb;
}

const toLinear = (c: number) => {
  const v = c / 255;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
};
const toSrgb = (v: number) =>
  255 * (v <= 0.0031308 ? v * 12.92 : 1.055 * v ** (1 / 2.4) - 0.055);
const clamp = (v: number, max: number) => Math.min(max, Math.max(0, v));

function saturateMatrix([r, g, b]: Rgb, s: number, max: number): Rgb {
  return [
    clamp(
      (0.213 + 0.787 * s) * r +
        (0.715 - 0.715 * s) * g +
        (0.072 - 0.072 * s) * b,
      max,
    ),
    clamp(
      (0.213 - 0.213 * s) * r +
        (0.715 + 0.285 * s) * g +
        (0.072 - 0.072 * s) * b,
      max,
    ),
    clamp(
      (0.213 - 0.213 * s) * r +
        (0.715 - 0.715 * s) * g +
        (0.072 + 0.928 * s) * b,
      max,
    ),
  ];
}

function saturateVariants(c: Rgb, s: number): Rgb[] {
  const lin = saturateMatrix(c.map(toLinear) as unknown as Rgb, s, 1);
  return [c, saturateMatrix(c, s, 255), lin.map(toSrgb) as unknown as Rgb];
}

function luminance(c: Rgb): number {
  const [r, g, b] = c.map(toLinear) as unknown as Rgb;
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [
    number,
    number,
  ];
  return (hi + 0.05) / (lo + 0.05);
}

const TEXT_TOKENS = [
  "--text",
  "--text-muted",
  "--accent",
  "--ok",
  "--warn",
  "--error",
] as const;

function surfaces(t: Map<string, string>): Array<{ name: string; rgb: Rgb }> {
  const get = (k: string) => {
    const v = t.get(k);
    if (!v) throw new Error(`missing token ${k}`);
    return v;
  };
  const backdrop = gradientStops(get("--bg"));
  const glass = parseColor(get("--glass-bg"));
  const chip = parseColor(get("--chip-bg"));
  const solid = parseColor(get("--glass-solid")).rgb;
  const out: Array<{ name: string; rgb: Rgb }> = [
    { name: "solid", rgb: solid },
    { name: "chip/solid", rgb: over(chip, solid) },
  ];
  const unders: Array<{ name: string; rgb: Rgb }> = backdrop.map((rgb, i) => ({
    name: `bg${i}`,
    rgb,
  }));
  for (const blob of ["--blob-1", "--blob-2", "--blob-3"]) {
    backdrop.forEach((stop, i) => {
      unders.push({
        name: `${blob}/bg${i}`,
        rgb: over(parseColor(get(blob)), stop),
      });
    });
  }
  for (const under of unders) {
    saturateVariants(under.rgb, 1.7).forEach((sat, v) => {
      const g = over(glass, sat);
      out.push({ name: `glass/${under.name}/sat${v}`, rgb: g });
      out.push({
        name: `chip/glass/${under.name}/sat${v}`,
        rgb: over(chip, g),
      });
    });
  }
  return out;
}

const THEMES = {
  light: tokens(":root"),
  dark: tokens('[data-theme="dark"]'),
};

describe("design token contrast (WCAG AA, 4.5:1)", () => {
  for (const [theme, t] of Object.entries(THEMES)) {
    for (const token of TEXT_TOKENS) {
      it(`${theme} ${token} >= 4.5 on every glass, chip and solid surface`, () => {
        const fg = parseColor(t.get(token) ?? "").rgb;
        const worst = surfaces(t)
          .map((s) => ({ ...s, ratio: contrast(fg, s.rgb) }))
          .sort((a, b) => a.ratio - b.ratio)[0];
        // A failure names the ratio and the surface that broke it.
        const verdict =
          worst && worst.ratio >= 4.5
            ? "AA"
            : `${worst?.ratio.toFixed(2)}:1 on ${worst?.name}`;
        expect(verdict).toBe("AA");
      });
    }
  }

  it("system-dark tokens mirror the explicit dark theme", () => {
    const system = tokens(
      ':root:not([data-theme="light"]):not([data-theme="dark"])',
    );
    for (const [name, value] of THEMES.dark) {
      expect([name, system.get(name)]).toEqual([name, value]);
    }
  });
});

/** sRGB samples along a two-stop gradient (stops plus quarter points). */
function gradientSamples(value: string): Rgb[] {
  const [a, b] = gradientStops(value);
  if (!a || !b) throw new Error(`need two stops in ${value}`);
  return [0, 0.25, 0.5, 0.75, 1].map(
    (t) => a.map((c, i) => c + ((b[i] ?? 0) - c) * t) as unknown as Rgb,
  );
}

describe("send button icon (WCAG 1.4.11 non-text, 3:1)", () => {
  for (const [theme, t] of Object.entries(THEMES)) {
    it(`${theme} white icon >= 3 across --accent-fill`, () => {
      const worst = Math.min(
        ...gradientSamples(t.get("--accent-fill") ?? "").map((c) =>
          contrast([255, 255, 255], c),
        ),
      );
      expect(Number(worst.toFixed(2))).toBeGreaterThanOrEqual(3);
    });
  }
});

/** `color-mix(in srgb, <c> <pct>%, <base>)` with an opaque base. */
function mix(c: Rgb, base: Rgb, pct: number): Rgb {
  const p = pct / 100;
  return c.map((v, i) => v * p + (base[i] ?? 0) * (1 - p)) as unknown as Rgb;
}

/*
 * The code view (and its diff) sits on the opaque --glass-solid well, so
 * diff colours are checked on the line tints layered over that solid.
 */
describe("code diff lines (WCAG AA, 4.5:1)", () => {
  for (const [theme, t] of Object.entries(THEMES)) {
    const solid = parseColor(t.get("--glass-solid") ?? "").rgb;
    for (const [tint, marker] of [
      ["--diff-add-bg", "--diff-add"],
      ["--diff-del-bg", "--diff-del"],
    ] as const) {
      for (const fg of ["--text", marker]) {
        it(`${theme} ${fg} on ${tint}`, () => {
          const bg = over(parseColor(t.get(tint) ?? ""), solid);
          const ratio = contrast(parseColor(t.get(fg) ?? "").rgb, bg);
          expect(Number(ratio.toFixed(2))).toBeGreaterThanOrEqual(4.5);
        });
      }
    }
  }
});

/** Keep in step with `.plan-node` fills in globals.css. */
const PLAN_TINT_PCT = 16;

describe("plan node fills (WCAG AA, 4.5:1)", () => {
  for (const [theme, t] of Object.entries(THEMES)) {
    const solid = parseColor(t.get("--glass-solid") ?? "").rgb;
    const fills: Array<[string, Rgb]> = [
      ["pending (solid)", solid],
      ...["--accent", "--warn", "--ok", "--error"].map((k): [string, Rgb] => [
        k,
        mix(parseColor(t.get(k) ?? "").rgb, solid, PLAN_TINT_PCT),
      ]),
    ];
    for (const [name, fill] of fills) {
      for (const fg of ["--text", "--text-muted"]) {
        it(`${theme} ${fg} on ${name} node`, () => {
          const ratio = contrast(parseColor(t.get(fg) ?? "").rgb, fill);
          expect(Number(ratio.toFixed(2))).toBeGreaterThanOrEqual(4.5);
        });
      }
    }
  }
});

/** OKLab (×100) of an sRGB colour, for perceptual distance between marks. */
function oklab(c: Rgb): Rgb {
  const [r, g, b] = c.map(toLinear) as unknown as Rgb;
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  return [
    100 * (0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s),
    100 * (1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s),
    100 * (0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s),
  ];
}

const deltaE = (a: Rgb, b: Rgb) =>
  Math.hypot(...oklab(a).map((v, i) => v - (oklab(b)[i] ?? 0)));

/** Keep in step with CHART_TOKENS in src/components/evals/chart-colours.ts. */
const CHART_SERIES_TOKENS = [
  "--chart-baseline",
  "--chart-router-1",
  "--chart-router-2",
] as const;

/*
 * Evals chart marks (points, lines) sit on a glass card: each series colour
 * must reach 3:1 against every glass/solid surface (WCAG 1.4.11), and every
 * pair of series must stay apart (OKLab ΔE >= 15) so no two read as one.
 */
describe("chart series colours (WCAG 1.4.11 non-text, 3:1)", () => {
  for (const [theme, t] of Object.entries(THEMES)) {
    for (const token of CHART_SERIES_TOKENS) {
      it(`${theme} ${token} >= 3 on every glass and solid surface`, () => {
        const fg = parseColor(t.get(token) ?? "").rgb;
        const worst = surfaces(t)
          .map((s) => ({ ...s, ratio: contrast(fg, s.rgb) }))
          .sort((a, b) => a.ratio - b.ratio)[0];
        const verdict =
          worst && worst.ratio >= 3
            ? "3:1"
            : `${worst?.ratio.toFixed(2)}:1 on ${worst?.name}`;
        expect(verdict).toBe("3:1");
      });
    }

    it(`${theme} chart series are pairwise distinguishable`, () => {
      const colours = CHART_SERIES_TOKENS.map(
        (k) => [k, parseColor(t.get(k) ?? "").rgb] as const,
      );
      for (const [i, [a, ca]] of colours.entries()) {
        for (const [b, cb] of colours.slice(i + 1)) {
          expect([a, b, Number(deltaE(ca, cb).toFixed(1)) >= 15]).toEqual([
            a,
            b,
            true,
          ]);
        }
      }
    });
  }
});
