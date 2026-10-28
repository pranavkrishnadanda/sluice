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
