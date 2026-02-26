# HTML to Figma Layers

A two-part tool that converts any live website into native, editable Figma layers with auto layout, color variables, text styles, and font handling.

---

## Architecture

```
extractor/
  extract.py         ← Python/Playwright script — runs on your machine
plugin/
  code.js            ← Figma plugin main thread
  ui.html            ← Figma plugin UI (runs in iframe)
  manifest.json      ← Figma plugin manifest
```

**Why two parts?** Figma plugins cannot run a headless browser. The Python extractor uses Playwright to load and render the page, walks the fully-computed DOM tree, collects styles, and also downloads all `@font-face` font files. Everything is serialised into a single JSON file that the Figma plugin consumes.

---

## Requirements

### Python extractor

```
pip install playwright requests
playwright install chromium
```

### Figma plugin

Load the `plugin/` folder in Figma via **Plugins → Development → Import plugin from manifest**.

---

## Usage

### Step 1 — Extract a website

```bash
python extractor/extract.py https://example.com
```

This produces a JSON file (e.g. `example_com_1440.json`) in the `extractor/` directory.

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--width 390` | `1440` | Viewport width in pixels (use < 500 for mobile UA) |
| `--dark` | off | Use dark color scheme |
| `--selector "#app"` | `body` | Root CSS selector to extract |
| `--wait 5` | `3.0` | Extra seconds to wait after network idle |
| `--no-scroll` | off | Skip scroll-to-load lazy content |
| `--no-fonts` | off | Skip font downloading |
| `--output out.json` | auto | Override output file path |

**Examples:**

```bash
# Desktop, full page
python extractor/extract.py https://stripe.com

# Mobile viewport
python extractor/extract.py https://stripe.com --width 390

# Only a specific section
python extractor/extract.py https://stripe.com --selector ".hero-section"

# Dark mode
python extractor/extract.py https://github.com --dark

# Skip font download (faster, but fonts tab in plugin will be empty)
python extractor/extract.py https://example.com --no-fonts
```

### Step 2 — Import in Figma

1. Open the **HTML to Figma Layers** plugin in Figma
2. On the **Upload JSON** tab, drag & drop or browse to the JSON file
3. Switch to the **Fonts** tab to review which fonts are available in Figma and which are missing
4. Install any missing fonts if needed (see Font Handling below)
5. Click **Import to Figma**

---

## What gets created

| Element | Figma node |
|---------|-----------|
| `<div>`, `<section>`, `<nav>`, etc. | Frame with auto layout (vertical/horizontal) |
| `<img>`, `<picture>` | Rectangle with IMAGE fill (fetched live) |
| `<svg>` elements | Rectangle placeholder (purple tint) |
| Text content | Text layer with matched font + style |
| `display: flex` | Frame with HORIZONTAL or VERTICAL auto layout |
| `display: grid` | Frame (fixed, grid layout mapped best-effort) |
| `box-shadow` | DROP_SHADOW effect |
| `border-radius` | Per-corner radius |
| `border` | Stroke with INSIDE alignment |
| `opacity` | Layer opacity |
| Top 50 colours | Color Variables collection "Imported Colors" |
| Top 30 font styles | Text Styles |

---

## Font Handling

### How it works

The extractor scans every loaded `@font-face` rule from `document.styleSheets` and also detects Google Fonts `<link>` tags. For each unique font variant (family + weight + style), it:

1. Picks the best format (prefers `woff2 > woff > ttf`)
2. Downloads the font binary
3. Base64-encodes it and embeds it in the JSON under `fontFiles`

The JSON `fontFiles` array looks like:

```json
{
  "fontFiles": [
    {
      "family": "Inter",
      "weight": 400,
      "style": "normal",
      "format": "woff2",
      "url": "https://fonts.gstatic.com/s/inter/...",
      "data": "d09GRg...",
      "source": "google"
    }
  ]
}
```

### Plugin font resolution

When building layers, the plugin:

1. Calls `figma.listAvailableFontsAsync()` to get the complete list of fonts available in this Figma environment (includes fonts installed on your machine via Figma Font Helper, Figma's built-in fonts, and org fonts)
2. For each text node, checks if the required family + weight is available
3. If available: uses it directly
4. If the family exists but the exact weight is missing: finds the closest available weight
5. If the family is entirely absent: picks a sensible fallback from the same category (sans-serif → Inter/Roboto; serif → Georgia/Merriweather; mono → Courier New/Roboto Mono) and records it as a **missing font**

### Fonts tab

The **Fonts** tab in the plugin UI shows:

- **Font Availability** — each font family used on the page, colour-coded green (available) or amber (missing), with the fallback family shown
- **Embedded Font Files** — the fonts downloaded by the extractor, with file sizes
- **Download Missing Fonts** — downloads only the fonts that were substituted during import
- **Download All Fonts** — downloads every embedded font

### Installing missing fonts

To get pixel-perfect font matching:

1. Click **Download Missing Fonts** in the Fonts tab
2. Install the downloaded `.woff2`/`.ttf` files on your operating system
3. Restart the [Figma desktop app](https://www.figma.com/downloads/) or use the [Figma Font Helper](https://www.figma.com/downloads/) for browser-based Figma
4. Re-run the import — the fonts will now be found

---

## JSON output format

```jsonc
{
  "url": "https://example.com",
  "title": "Example Domain",
  "viewport": { "width": 1440, "height": 900 },
  "colors": [
    { "hex": "#0d99ff", "count": 42 }
  ],
  "fonts": [
    { "family": "Inter", "weight": 400, "size": 16, "lineHeight": 24, "count": 87 }
  ],
  "fontFiles": [
    {
      "family": "Inter",
      "weight": 400,
      "style": "normal",
      "format": "woff2",
      "url": "https://...",
      "data": "base64...",
      "source": "google"
    }
  ],
  "tree": {
    "id": 1,
    "type": "FRAME",
    "tag": "body",
    "name": "body",
    "x": 0, "y": 0, "width": 1440, "height": 900,
    "layoutMode": "VERTICAL",
    "autoLayout": { "direction": "VERTICAL", "gap": 0, ... },
    "padding": { "top": 0, "right": 0, "bottom": 0, "left": 0 },
    "border": { "width": {...}, "color": null, "radius": [0,0,0,0] },
    "backgroundColor": { "r": 1, "g": 1, "b": 1, "a": 1 },
    "children": [ ... ]
  }
}
```

---

## Manifest networkAccess

The plugin manifest requests `"allowedDomains": ["*"]` so that `figma.createImageAsync(url)` can fetch image assets from any domain. This is required for images to appear in the imported layers.

---

## Known limitations

- **SVGs** are created as placeholder rectangles (light purple). Full SVG vectorisation would require a separate API call.
- **CSS Grid** layout is approximated as fixed positioning. Figma's auto layout does not map 1:1 to CSS Grid.
- **Custom fonts** cannot be programmatically installed into Figma — only fonts already installed on your machine (or Figma's built-in set) can be used. Use the Download buttons + Figma Font Helper to bridge this gap.
- **Dynamic content** (JavaScript-rendered, infinite scroll, etc.) is captured if it loads within the `--wait` window and the scroll pass.
- **iframes** and cross-origin content inside the page are skipped.
- **Videos / canvas** are skipped (cannot be represented as Figma layers).

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `playwright install` error | Run `playwright install chromium` after installing playwright |
| Font substituted with Inter | Install missing font via OS, use Figma Font Helper |
| Images are grey placeholders | Image URL may be behind auth or blocked by CORS |
| Extraction times out | Increase `--wait`, or use `--no-scroll` for heavy pages |
| `requests` not found | `pip install requests` |
| JSON is very large | Use `--no-fonts` to skip font embedding; fonts will be amber in the report |
