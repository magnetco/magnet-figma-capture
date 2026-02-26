#!/usr/bin/env python3
"""
DOM Extractor for HTML-to-Figma Plugin
=======================================
Uses Playwright to load a URL, walk the DOM tree, and extract
computed styles + layout info into a JSON structure that the
Figma plugin can consume to create native editable layers.

Also downloads all @font-face fonts and embeds them as base64
in the JSON output under a "fontFiles" key.

Usage:
    python extract.py <url> [--width 1440] [--dark] [--output file.json]
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import sync_playwright

# JavaScript that runs in-page to walk the DOM and extract everything
EXTRACTION_SCRIPT = """
(rootSelector) => {
    const SKIP_TAGS = new Set([
        'SCRIPT', 'STYLE', 'NOSCRIPT', 'META', 'LINK', 'HEAD',
        'BR', 'WBR', 'TEMPLATE', 'SLOT', 'IFRAME', 'OBJECT', 'EMBED',
        'SVG', 'CANVAS', 'VIDEO', 'AUDIO', 'MAP', 'SOURCE', 'TRACK',
        'DIALOG', 'PORTAL'
    ]);

    const INLINE_TAGS = new Set([
        'SPAN', 'A', 'STRONG', 'B', 'EM', 'I', 'U', 'S', 'SMALL',
        'SUB', 'SUP', 'MARK', 'ABBR', 'CODE', 'KBD', 'SAMP', 'VAR',
        'TIME', 'Q', 'CITE', 'DFN', 'LABEL', 'DATA', 'OUTPUT'
    ]);

    // Unique ID counter
    let idCounter = 0;

    // Collected colors and fonts for variables/styles
    const colorsFound = new Map(); // hex -> count
    const fontsFound = new Map();  // "family|weight|size" -> count

    function rgbToHex(r, g, b) {
        return '#' + [r, g, b].map(x => {
            const hex = Math.round(x).toString(16);
            return hex.length === 1 ? '0' + hex : hex;
        }).join('');
    }

    function parseColor(colorStr) {
        if (!colorStr || colorStr === 'transparent' || colorStr === 'rgba(0, 0, 0, 0)') {
            return null;
        }
        const match = colorStr.match(/rgba?\\([\\d.]+,\\s*[\\d.]+,\\s*[\\d.]+(?:,\\s*[\\d.]+)?\\)/);
        if (match) {
            const parts = colorStr.match(/rgba?\\(([\\d.]+),\\s*([\\d.]+),\\s*([\\d.]+)(?:,\\s*([\\d.]+))?\\)/);
            if (parts) {
                const r = parseFloat(parts[1]) / 255;
                const g = parseFloat(parts[2]) / 255;
                const b = parseFloat(parts[3]) / 255;
                const a = parts[4] !== undefined ? parseFloat(parts[4]) : 1;
                return { r, g, b, a };
            }
        }
        return null;
    }

    function trackColor(colorStr) {
        const parsed = parseColor(colorStr);
        if (parsed && parsed.a > 0) {
            const hex = rgbToHex(parsed.r * 255, parsed.g * 255, parsed.b * 255);
            colorsFound.set(hex, (colorsFound.get(hex) || 0) + 1);
        }
        return parsed;
    }

    function trackFont(family, weight, size, lineHeight) {
        const key = `${family}|${weight}|${size}|${lineHeight}`;
        fontsFound.set(key, (fontsFound.get(key) || 0) + 1);
        return key;
    }

    function isElementVisible(el, cs) {
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        if (cs.opacity === '0') return false;
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 && rect.height <= 0) return false;
        return true;
    }

    function getTextContent(el) {
        // Get direct text nodes only (not children's text)
        let text = '';
        for (const child of el.childNodes) {
            if (child.nodeType === Node.TEXT_NODE) {
                const t = child.textContent.trim();
                if (t) text += (text ? ' ' : '') + t;
            }
        }
        return text;
    }

    function extractNode(el, depth) {
        if (depth > 30) return null; // safety limit
        if (el.nodeType !== Node.ELEMENT_NODE) return null;
        if (SKIP_TAGS.has(el.tagName)) return null;

        const cs = window.getComputedStyle(el);
        if (!isElementVisible(el, cs)) return null;

        const rect = el.getBoundingClientRect();
        const id = ++idCounter;

        // --- Determine node type ---
        const tagName = el.tagName;
        const isImg = tagName === 'IMG' || tagName === 'PICTURE';
        const isInput = tagName === 'INPUT' || tagName === 'TEXTAREA' || tagName === 'SELECT';
        const isSvg = tagName === 'svg' || el.closest('svg');
        const directText = getTextContent(el);
        const hasChildElements = el.querySelector(':scope > *') !== null;

        let nodeType = 'FRAME'; // default: treat as frame
        if (isImg) {
            nodeType = 'IMAGE';
        } else if (isSvg) {
            nodeType = 'VECTOR';
        } else if (directText && !hasChildElements) {
            nodeType = 'TEXT';
        } else if (directText && hasChildElements) {
            nodeType = 'FRAME'; // mixed content: frame with text + child nodes
        }

        // --- Layout / Auto Layout detection ---
        const display = cs.display;
        const flexDirection = cs.flexDirection;
        const justifyContent = cs.justifyContent;
        const alignItems = cs.alignItems;
        const flexWrap = cs.flexWrap;
        const gap = cs.gap;
        const rowGap = cs.rowGap;
        const columnGap = cs.columnGap;
        const position = cs.position;
        const overflowX = cs.overflowX;
        const overflowY = cs.overflowY;

        let layoutMode = 'NONE';
        let autoLayoutProps = null;

        if (display === 'flex' || display === 'inline-flex') {
            layoutMode = flexDirection.startsWith('column') ? 'VERTICAL' : 'HORIZONTAL';
            autoLayoutProps = {
                direction: layoutMode,
                justifyContent: justifyContent,
                alignItems: alignItems,
                flexWrap: flexWrap,
                gap: parseFloat(gap) || 0,
                rowGap: parseFloat(rowGap) || 0,
                columnGap: parseFloat(columnGap) || 0,
            };
        } else if (display === 'grid' || display === 'inline-grid') {
            layoutMode = 'GRID';
            autoLayoutProps = {
                direction: 'GRID',
                gap: parseFloat(gap) || 0,
                rowGap: parseFloat(rowGap) || 0,
                columnGap: parseFloat(columnGap) || 0,
                gridTemplateColumns: cs.gridTemplateColumns,
                gridTemplateRows: cs.gridTemplateRows,
            };
        } else if (display === 'block' || display === 'flow-root' || display === 'list-item') {
            // Block elements with multiple children often act like vertical flex
            const childEls = Array.from(el.children).filter(c => {
                const ccs = window.getComputedStyle(c);
                return ccs.display !== 'none' && ccs.position !== 'absolute' && ccs.position !== 'fixed';
            });
            if (childEls.length > 1) {
                layoutMode = 'VERTICAL';
                autoLayoutProps = {
                    direction: 'VERTICAL',
                    justifyContent: 'flex-start',
                    alignItems: 'stretch',
                    flexWrap: 'nowrap',
                    gap: 0,
                    rowGap: 0,
                    columnGap: 0,
                    inferred: true,
                };
            }
        }

        // --- Box model ---
        const paddingTop = parseFloat(cs.paddingTop) || 0;
        const paddingRight = parseFloat(cs.paddingRight) || 0;
        const paddingBottom = parseFloat(cs.paddingBottom) || 0;
        const paddingLeft = parseFloat(cs.paddingLeft) || 0;

        const borderTopWidth = parseFloat(cs.borderTopWidth) || 0;
        const borderRightWidth = parseFloat(cs.borderRightWidth) || 0;
        const borderBottomWidth = parseFloat(cs.borderBottomWidth) || 0;
        const borderLeftWidth = parseFloat(cs.borderLeftWidth) || 0;

        const borderRadius = [
            parseFloat(cs.borderTopLeftRadius) || 0,
            parseFloat(cs.borderTopRightRadius) || 0,
            parseFloat(cs.borderBottomRightRadius) || 0,
            parseFloat(cs.borderBottomLeftRadius) || 0,
        ];

        // --- Colors ---
        const bgColor = trackColor(cs.backgroundColor);
        const textColor = trackColor(cs.color);
        const borderColor = trackColor(cs.borderColor || cs.borderTopColor);

        // Background image
        let backgroundImage = null;
        if (cs.backgroundImage && cs.backgroundImage !== 'none') {
            const urlMatch = cs.backgroundImage.match(/url\\(["']?([^"')]+)["']?\\)/);
            if (urlMatch) {
                backgroundImage = urlMatch[1];
            }
        }

        // --- Typography ---
        let textProps = null;
        if (nodeType === 'TEXT' || directText) {
            const fontFamily = cs.fontFamily.split(',')[0].trim().replace(/['"]/g, '');
            const fontWeight = parseInt(cs.fontWeight) || 400;
            const fontSize = parseFloat(cs.fontSize) || 16;
            const lineHeight = parseFloat(cs.lineHeight) || fontSize * 1.2;
            const letterSpacing = parseFloat(cs.letterSpacing) || 0;
            const textAlign = cs.textAlign;
            const textDecoration = cs.textDecorationLine;
            const textTransform = cs.textTransform;

            trackFont(fontFamily, fontWeight, fontSize, lineHeight);

            textProps = {
                characters: directText,
                fontFamily: fontFamily,
                fontWeight: fontWeight,
                fontSize: fontSize,
                lineHeight: lineHeight,
                letterSpacing: letterSpacing,
                textAlign: textAlign,
                textDecoration: textDecoration,
                textTransform: textTransform,
                color: textColor,
            };
        }

        // --- Image source ---
        let imageSrc = null;
        if (isImg) {
            imageSrc = el.src || el.currentSrc || null;
            // Also check for srcset
            if (!imageSrc && el.srcset) {
                imageSrc = el.srcset.split(',')[0].trim().split(' ')[0];
            }
        }

        // --- Opacity & effects ---
        const opacity = parseFloat(cs.opacity) || 1;
        const boxShadow = cs.boxShadow !== 'none' ? cs.boxShadow : null;

        // --- Sizing ---
        const flexGrow = parseFloat(cs.flexGrow) || 0;
        const flexShrink = parseFloat(cs.flexShrink) || 1;
        const flexBasis = cs.flexBasis;
        const alignSelf = cs.alignSelf;
        const width = rect.width;
        const height = rect.height;
        const maxWidth = cs.maxWidth;
        const minWidth = cs.minWidth;

        // --- Build node ---
        const node = {
            id: id,
            type: nodeType,
            tag: tagName.toLowerCase(),
            name: el.id || el.className?.toString().split(' ')[0] || tagName.toLowerCase(),
            x: rect.left,
            y: rect.top,
            width: width,
            height: height,
            opacity: opacity,

            // Layout
            layoutMode: layoutMode,
            autoLayout: autoLayoutProps,
            position: position,

            // Box model
            padding: { top: paddingTop, right: paddingRight, bottom: paddingBottom, left: paddingLeft },
            border: {
                width: { top: borderTopWidth, right: borderRightWidth, bottom: borderBottomWidth, left: borderLeftWidth },
                color: borderColor,
                radius: borderRadius,
            },

            // Fills
            backgroundColor: bgColor,
            backgroundImage: backgroundImage,

            // Sizing
            flexGrow: flexGrow,
            flexShrink: flexShrink,
            flexBasis: flexBasis,
            alignSelf: alignSelf,

            // Overflow
            clipContent: overflowX === 'hidden' || overflowY === 'hidden',

            // Effects
            boxShadow: boxShadow,

            // Text (if applicable)
            text: textProps,

            // Image (if applicable)
            imageSrc: imageSrc,

            // Children
            children: [],
        };

        // --- Recurse children ---
        if (nodeType !== 'TEXT' && !isImg && !isSvg) {
            // Handle mixed content: if there's direct text AND child elements,
            // insert a text node before child elements
            if (directText && hasChildElements) {
                const textNode = {
                    id: ++idCounter,
                    type: 'TEXT',
                    tag: '_text',
                    name: 'text',
                    x: rect.left + paddingLeft,
                    y: rect.top + paddingTop,
                    width: width - paddingLeft - paddingRight,
                    height: 0, // will be determined by content
                    opacity: 1,
                    layoutMode: 'NONE',
                    autoLayout: null,
                    position: 'static',
                    padding: { top: 0, right: 0, bottom: 0, left: 0 },
                    border: { width: { top: 0, right: 0, bottom: 0, left: 0 }, color: null, radius: [0,0,0,0] },
                    backgroundColor: null,
                    backgroundImage: null,
                    flexGrow: 0,
                    flexShrink: 1,
                    flexBasis: 'auto',
                    alignSelf: 'auto',
                    clipContent: false,
                    boxShadow: null,
                    text: {
                        characters: directText,
                        fontFamily: cs.fontFamily.split(',')[0].trim().replace(/['"]/g, ''),
                        fontWeight: parseInt(cs.fontWeight) || 400,
                        fontSize: parseFloat(cs.fontSize) || 16,
                        lineHeight: parseFloat(cs.lineHeight) || 16 * 1.2,
                        letterSpacing: parseFloat(cs.letterSpacing) || 0,
                        textAlign: cs.textAlign,
                        textDecoration: cs.textDecorationLine,
                        textTransform: cs.textTransform,
                        color: textColor,
                    },
                    imageSrc: null,
                    children: [],
                };
                node.children.push(textNode);
            }

            for (const child of el.children) {
                const childNode = extractNode(child, depth + 1);
                if (childNode) {
                    node.children.push(childNode);
                }
            }
        }

        return node;
    }

    // Start extraction
    const root = document.querySelector(rootSelector || 'body');
    if (!root) return { error: 'Root element not found' };

    const tree = extractNode(root, 0);

    // Build color palette and font catalog
    const colors = Array.from(colorsFound.entries())
        .sort((a, b) => b[1] - a[1])
        .map(([hex, count]) => ({ hex, count }));

    const fonts = Array.from(fontsFound.entries())
        .sort((a, b) => b[1] - a[1])
        .map(([key, count]) => {
            const [family, weight, size, lineHeight] = key.split('|');
            return {
                family, weight: parseInt(weight),
                size: parseFloat(size), lineHeight: parseFloat(lineHeight),
                count
            };
        });

    return {
        tree: tree,
        colors: colors,
        fonts: fonts,
        viewport: { width: window.innerWidth, height: window.innerHeight },
        url: window.location.href,
        title: document.title,
    };
}
"""

# JavaScript to extract @font-face rules from the page
FONT_FACE_SCRIPT = """
() => {
    const fontFaces = [];
    for (const sheet of document.styleSheets) {
        try {
            for (const rule of sheet.cssRules) {
                if (rule instanceof CSSFontFaceRule) {
                    const family = rule.style.getPropertyValue('font-family').replace(/['"]/g, '').trim();
                    const weight = rule.style.getPropertyValue('font-weight') || '400';
                    const style = rule.style.getPropertyValue('font-style') || 'normal';
                    const src = rule.style.getPropertyValue('src');
                    // Extract ALL URLs from src (may have multiple formats)
                    const urlMatches = [...src.matchAll(/url\\(["']?([^"')]+)["']?\\)(?:\\s+format\\(["']?([^"')]+)["']?\\))?/g)];
                    for (const m of urlMatches) {
                        const url = m[1];
                        const format = m[2] || null;
                        // Prefer woff2, then woff, then ttf
                        fontFaces.push({ family, weight, style, url, format, type: 'face' });
                    }
                }
            }
        } catch (e) { /* cross-origin stylesheet — skip */ }
    }

    // Also detect Google Fonts <link> tags
    const links = document.querySelectorAll('link[href*="fonts.googleapis.com"]');
    links.forEach(link => {
        fontFaces.push({ googleCssUrl: link.href, type: 'google' });
    });

    return fontFaces;
}
"""


def _preferred_font_url(faces):
    """
    Given a list of font-face entries for the same family+weight+style,
    pick the best URL: prefer woff2 > woff > ttf > others.
    """
    FORMAT_RANK = {"woff2": 0, "woff": 1, "truetype": 2, "opentype": 3}
    def rank(f):
        fmt = (f.get("format") or "").lower()
        url = f.get("url", "").lower()
        if ".woff2" in url or fmt == "woff2":
            return 0
        if ".woff" in url or fmt == "woff":
            return 1
        if ".ttf" in url or fmt in ("truetype", "opentype"):
            return 2
        return 9
    return sorted(faces, key=rank)[0]["url"]


def _resolve_google_font_urls(css_url, session, page_url):
    """
    Fetch a Google Fonts CSS URL and parse out the actual font file URLs.
    Returns a list of dicts: {family, weight, style, url, format}.
    """
    results = []
    try:
        # Use a modern browser UA so Google Fonts returns woff2
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
        resp = session.get(css_url, headers=headers, timeout=15)
        resp.raise_for_status()
        css_text = resp.text

        # Parse the CSS for @font-face blocks
        # Pattern: /* family */ ... font-family: ...; font-style: ...; font-weight: ...; src: url(...) format(...)
        block_pattern = re.compile(
            r'@font-face\s*\{([^}]+)\}', re.DOTALL
        )
        url_pattern = re.compile(
            r'url\(["\']?([^"\')\s]+)["\']?\)\s*format\(["\']?([^"\')\s]+)["\']?\)'
        )
        family_pattern = re.compile(r"font-family:\s*['\"]?([^;'\"]+)['\"]?;")
        weight_pattern = re.compile(r"font-weight:\s*(\d+);")
        style_pattern = re.compile(r"font-style:\s*(\w+);")

        for block in block_pattern.finditer(css_text):
            content = block.group(1)
            family_m = family_pattern.search(content)
            weight_m = weight_pattern.search(content)
            style_m = style_pattern.search(content)
            url_m = url_pattern.search(content)

            if family_m and url_m:
                results.append({
                    "family": family_m.group(1).strip(),
                    "weight": weight_m.group(1) if weight_m else "400",
                    "style": style_m.group(1) if style_m else "normal",
                    "url": url_m.group(1),
                    "format": url_m.group(2),
                    "source": "google",
                })
    except Exception as e:
        print(f"  Warning: could not fetch Google Fonts CSS {css_url}: {e}")
    return results


def download_fonts(page, page_url):
    """
    Extract all @font-face URLs from the page, download each font file,
    and return a list of font file dicts with base64-encoded data.

    Returns:
        list of {
            family, weight, style, format, url,
            data (base64 string), source ("css" | "google")
        }
    """
    print("  Extracting font-face rules...")
    try:
        raw_faces = page.evaluate(FONT_FACE_SCRIPT)
    except Exception as e:
        print(f"  Warning: font extraction JS failed: {e}")
        return []

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    })

    # Separate direct font URLs from Google Fonts CSS links
    css_faces = [f for f in raw_faces if f.get("type") == "face"]
    google_links = [f for f in raw_faces if f.get("type") == "google"]

    # Resolve Google Fonts CSS -> actual font URLs
    google_faces = []
    for link in google_links:
        gurl = link.get("googleCssUrl", "")
        print(f"  Resolving Google Fonts CSS: {gurl[:80]}...")
        google_faces.extend(_resolve_google_font_urls(gurl, session, page_url))

    all_faces = []

    # Group direct CSS @font-face entries by family+weight+style and pick best format
    grouped = {}
    for f in css_faces:
        key = (f["family"], str(f.get("weight", "400")), f.get("style", "normal"))
        grouped.setdefault(key, []).append(f)

    for key, faces in grouped.items():
        best_url = _preferred_font_url(faces)
        # Resolve relative URLs
        abs_url = urljoin(page_url, best_url)
        all_faces.append({
            "family": key[0],
            "weight": key[1],
            "style": key[2],
            "url": abs_url,
            "format": (faces[0].get("format") or "").lower() or _guess_format(abs_url),
            "source": "css",
        })

    # Add Google Fonts (already have absolute URLs)
    # Deduplicate by family+weight+style; prefer woff2
    gf_grouped = {}
    for f in google_faces:
        key = (f["family"], str(f.get("weight", "400")), f.get("style", "normal"))
        gf_grouped.setdefault(key, []).append(f)

    for key, faces in gf_grouped.items():
        best = sorted(faces, key=lambda x: 0 if "woff2" in (x.get("format") or "") else 1)[0]
        all_faces.append({
            "family": key[0],
            "weight": key[1],
            "style": key[2],
            "url": best["url"],
            "format": best.get("format", "woff2"),
            "source": "google",
        })

    # Deduplicate by (family, weight, style) — css wins over google
    seen = {}
    deduped = []
    for f in all_faces:
        key = (f["family"].lower(), str(f["weight"]), f["style"])
        if key not in seen:
            seen[key] = True
            deduped.append(f)

    print(f"  Found {len(deduped)} unique font variants to download")

    # Download each font file and base64-encode it
    font_files = []
    for f in deduped:
        url = f["url"]
        family = f["family"]
        weight = f["weight"]
        style = f["style"]
        print(f"  Downloading font: {family} {weight} {style} ({url[:60]}...)")
        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
            encoded = base64.b64encode(resp.content).decode("ascii")
            font_files.append({
                "family": family,
                "weight": int(weight) if str(weight).isdigit() else weight,
                "style": style,
                "format": f.get("format") or _guess_format(url),
                "url": url,
                "data": encoded,
                "source": f.get("source", "css"),
            })
            print(f"    ✓ {family} {weight} {style} — {len(resp.content) // 1024} KB")
        except Exception as e:
            print(f"    ✗ Failed to download {url}: {e}")
            # Still include the entry without data so the UI can show it
            font_files.append({
                "family": family,
                "weight": int(weight) if str(weight).isdigit() else weight,
                "style": style,
                "format": f.get("format") or _guess_format(url),
                "url": url,
                "data": None,
                "source": f.get("source", "css"),
                "error": str(e),
            })

    return font_files


def _guess_format(url):
    """Guess font format from URL extension."""
    url_lower = url.lower().split("?")[0]
    if ".woff2" in url_lower:
        return "woff2"
    if ".woff" in url_lower:
        return "woff"
    if ".ttf" in url_lower:
        return "truetype"
    if ".otf" in url_lower:
        return "opentype"
    if ".eot" in url_lower:
        return "embedded-opentype"
    return "woff2"  # default assumption


def extract_dom(
    url: str,
    width: int = 1440,
    dark_mode: bool = False,
    root_selector: str = "body",
    wait_seconds: float = 3.0,
    scroll_to_load: bool = True,
    download_fonts_flag: bool = True,
):
    """Extract DOM tree with computed styles from a URL.
    
    Also downloads all @font-face font files and embeds them as base64
    in the output JSON under the 'fontFiles' key.
    """

    MOBILE_UA = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    )
    DESKTOP_UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    is_mobile = width < 500

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": width, "height": 900},
            device_scale_factor=2 if is_mobile else 1,
            color_scheme="dark" if dark_mode else "light",
            user_agent=MOBILE_UA if is_mobile else DESKTOP_UA,
            is_mobile=is_mobile,
            has_touch=is_mobile,
        )
        page = context.new_page()

        print(f"  Loading {url} at {width}px...")
        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
        except Exception:
            print("  networkidle timed out — falling back to domcontentloaded...")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                print("  domcontentloaded also timed out — using load event...")
                page.goto(url, wait_until="load", timeout=90000)
        page.wait_for_timeout(int(wait_seconds * 1000))

        # Scroll to trigger lazy loading
        if scroll_to_load:
            print("  Scrolling to load lazy content...")
            page.evaluate("""
                async () => {
                    await new Promise((resolve) => {
                        let totalHeight = 0;
                        const distance = 400;
                        const timer = setInterval(() => {
                            const scrollHeight = document.body.scrollHeight;
                            window.scrollBy(0, distance);
                            totalHeight += distance;
                            if (totalHeight >= scrollHeight) {
                                clearInterval(timer);
                                window.scrollTo(0, 0);
                                resolve();
                            }
                        }, 100);
                    });
                }
            """)
            page.wait_for_timeout(2000)

        # Try to dismiss cookie banners
        try:
            for selector in [
                '[class*="cookie"] button',
                '[class*="consent"] button',
                'button[aria-label="Close"]',
                'button[aria-label="Accept"]',
                'button[aria-label="Accept all"]',
            ]:
                btn = page.query_selector(selector)
                if btn and btn.is_visible():
                    btn.click()
                    page.wait_for_timeout(500)
                    break
        except Exception:
            pass

        # Extract DOM
        print("  Extracting DOM tree and computed styles...")
        result = page.evaluate(EXTRACTION_SCRIPT, root_selector)

        # Download fonts while the page is still open
        font_files = []
        if download_fonts_flag and "error" not in result:
            print("  Downloading font files...")
            font_files = download_fonts(page, url)

        context.close()
        browser.close()

    if "error" not in result:
        result["fontFiles"] = font_files

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract DOM tree for Figma import")
    parser.add_argument("url", help="URL to extract")
    parser.add_argument("--width", type=int, default=1440, help="Viewport width (default: 1440)")
    parser.add_argument("--dark", action="store_true", help="Use dark color scheme")
    parser.add_argument("--selector", default="body", help="Root CSS selector (default: body)")
    parser.add_argument("--wait", type=float, default=3.0, help="Wait time after load (seconds)")
    parser.add_argument("--output", "-o", type=str, default=None, help="Output JSON file path")
    parser.add_argument("--no-scroll", action="store_true", help="Skip scroll-to-load")
    parser.add_argument("--no-fonts", action="store_true", help="Skip font downloading")

    args = parser.parse_args()

    result = extract_dom(
        url=args.url,
        width=args.width,
        dark_mode=args.dark,
        root_selector=args.selector,
        wait_seconds=args.wait,
        scroll_to_load=not args.no_scroll,
        download_fonts_flag=not args.no_fonts,
    )

    if "error" in result:
        print(f"Error: {result['error']}")
        sys.exit(1)

    # Determine output path
    if args.output:
        out_path = args.output
    else:
        from urllib.parse import urlparse
        domain = urlparse(args.url).netloc.replace("www.", "")
        safe = re.sub(r'[^\w\-]', '_', domain)
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{safe}_{args.width}.json")

    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    node_count = 0
    def count_nodes(n):
        global node_count
        if n:
            node_count += 1
            for c in n.get("children", []):
                count_nodes(c)
    count_nodes(result.get("tree"))

    font_files = result.get("fontFiles", [])
    fonts_ok = sum(1 for f in font_files if f.get("data"))
    fonts_fail = len(font_files) - fonts_ok

    print(f"\n  Extracted {node_count} nodes")
    print(f"  {len(result.get('colors', []))} unique colors found")
    print(f"  {len(result.get('fonts', []))} unique font styles found")
    print(f"  {len(font_files)} font variants found — {fonts_ok} downloaded, {fonts_fail} failed")
    print(f"  Output: {out_path}")
