# Pinflow brand assets

Finalized logo system for **Pinflow** — PCB design copilot, by Faradworks.

## What's the design

- **Wordmark anatomy** — `PIN` in ink black, `FL` + glyph + `W` in royal violet.
- **Glyph** — replaces the O in FLOW. A filled royal disc with a white knockout trace + via passing through. Reads as PCB routing.
- **Typography** — DM Sans Bold (700), `-0.01em` letter-spacing.
- **Primary color** — Royal Violet `#6D28D9`.
- **Knockout color** — White on light; ink-black when used inverted on royal.

## Color tokens

| Token | Hex | Use |
|---|---|---|
| Royal | `#6D28D9` | Primary accent. Disc, FLOW letters. |
| Ink   | `#0B0E14` | PIN letters, dark backgrounds. |
| White | `#FFFFFF` | Knockouts, on-dark text. |
| Light surface | `#F4F5F7` | Light tile backgrounds. |

## File map

```
downloads/
├── svg/                            ← vector sources (editable)
│   ├── pinflow-wordmark.svg            Primary, royal on light
│   ├── pinflow-wordmark-dark.svg       For dark backgrounds
│   ├── pinflow-wordmark-mono-black.svg Single-color black
│   ├── pinflow-wordmark-mono-white.svg Single-color white (on dark)
│   ├── pinflow-mark.svg                Glyph only (royal disc, transparent bg)
│   ├── pinflow-mark-white.svg          Glyph only (white disc, royal details)
│   ├── pinflow-icon-dark.svg           Rounded tile, dark bg
│   ├── pinflow-icon-royal.svg          Rounded tile, royal bg (white disc inside)
│   ├── pinflow-icon-light.svg          Rounded tile, light bg
│   ├── pinflow-lockup.svg              Wordmark + tagline
│   └── pinflow-lockup-dark.svg         Lockup on dark
└── png/                            ← raster, ready to drop into slides/web
    ├── pinflow-wordmark-{512,1024,2048}.png
    ├── pinflow-wordmark-dark-{...}.png
    ├── pinflow-wordmark-mono-black-{...}.png
    ├── pinflow-wordmark-mono-white-{...}.png
    ├── pinflow-mark-{256,512,1024}.png         (transparent bg)
    ├── pinflow-icon-{dark,royal,light}-{...}.png
    ├── pinflow-favicon-{16,32,48,64,128}.png
    └── pinflow-lockup-{1024,2048}.png
```

## Usage notes

- **SVG files** reference DM Sans via Google Fonts `@import`. For pixel-perfect rendering, ensure DM Sans Bold (700) is available. For Adobe Illustrator import, outline the text first (`Type → Create Outlines`).
- **PNG files** are fully rendered — no font dependency.
- **Minimum size** — don't go below 14px height for the wordmark, or 16px for the mark alone.
- **Clearspace** — keep at least the width of the "P" in PIN as breathing room around the wordmark on all sides.
- **Don't** stretch, recolor outside the palette, or add effects (shadows, glows, outlines).

## Tagline

`PCB DESIGN COPILOT` — DM Sans Medium (500), `0.16em` tracking, uppercase, neutral gray `#6b7280` (or 50% white on dark).
