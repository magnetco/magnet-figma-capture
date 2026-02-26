// ============================================================
// HTML to Figma Layers — Plugin Code (code.js)
// ============================================================
// Receives a JSON DOM tree extracted by the companion script
// and creates native Figma nodes with auto layout, text styles,
// color variables, and a font availability report.
// ============================================================

figma.showUI(__html__, { width: 560, height: 680, themeColors: true });

// ─── State ───────────────────────────────────────────────────
let colorVariableCollection = null;
let colorVariables = {};        // hex -> Variable
let textStylesMap = {};         // key -> TextStyle
let imageCache = {};            // url -> ImageHash
let availableFontsMap = {};     // "Family|Style" -> true
let availableFamilies = {};     // "Family" -> Set of styles
let fontLoadErrors = {};        // family -> true (to avoid re-logging)
let missingFonts = {};          // "family|style" -> { family, style, usedBy: [] }
let stats = {
  frames: 0, texts: 0, images: 0, vectors: 0,
  colors: 0, textStyles: 0, fontsLoaded: 0, fontsMissing: 0
};

// ─── Message Handler ─────────────────────────────────────────
figma.ui.onmessage = async (msg) => {
  if (msg.type === 'import') {
    // Reset state for fresh import
    colorVariableCollection = null;
    colorVariables = {};
    textStylesMap = {};
    imageCache = {};
    availableFontsMap = {};
    availableFamilies = {};
    fontLoadErrors = {};
    missingFonts = {};
    stats = { frames: 0, texts: 0, images: 0, vectors: 0, colors: 0, textStyles: 0, fontsLoaded: 0, fontsMissing: 0 };

    try {
      const data = JSON.parse(msg.json);

      // Step 1: Build font availability map
      figma.ui.postMessage({ type: 'status', text: 'Scanning available fonts...' });
      await buildFontAvailabilityMap();

      // Step 2: Analyse which fonts are needed, send early font report
      const fontReport = buildFontReport(data);
      figma.ui.postMessage({ type: 'fontReport', report: fontReport, fontFiles: data.fontFiles || [] });

      // Step 3: Color variables
      figma.ui.postMessage({ type: 'status', text: 'Creating color variables...' });
      await createColorVariables(data.colors || []);

      // Step 4: Text styles
      figma.ui.postMessage({ type: 'status', text: 'Creating text styles...' });
      await createTextStyles(data.fonts || []);

      // Step 5: Build layers
      figma.ui.postMessage({ type: 'status', text: 'Building layers...' });
      const rootNode = await buildNode(data.tree, null);
      if (rootNode) {
        rootNode.name = data.title || data.url || 'Imported Page';
        figma.currentPage.appendChild(rootNode);
        figma.viewport.scrollAndZoomIntoView([rootNode]);
      }

      // Collect final missing fonts from text building
      const finalMissing = Object.values(missingFonts);
      stats.fontsMissing = finalMissing.length;

      figma.ui.postMessage({
        type: 'done',
        stats: stats,
        missingFonts: finalMissing,
        fontFiles: data.fontFiles || [],
      });

    } catch (e) {
      figma.ui.postMessage({ type: 'error', text: e.message || String(e) });
    }

  } else if (msg.type === 'cancel') {
    figma.closePlugin();
  }
};

// ─── Font Availability Map ────────────────────────────────────
async function buildFontAvailabilityMap() {
  try {
    const allFonts = await figma.listAvailableFontsAsync();
    for (const font of allFonts) {
      const key = `${font.fontName.family}|${font.fontName.style}`;
      availableFontsMap[key] = true;
      if (!availableFamilies[font.fontName.family]) {
        availableFamilies[font.fontName.family] = new Set();
      }
      availableFamilies[font.fontName.family].add(font.fontName.style);
    }
    console.log(`Font map built: ${allFonts.length} fonts available`);
  } catch (e) {
    console.warn('Could not list available fonts:', e);
  }
}

// ─── Font Report (pre-import analysis) ───────────────────────
function buildFontReport(data) {
  const neededFamilies = new Set();
  if (data.fonts) {
    for (const f of data.fonts) {
      neededFamilies.add(f.family);
    }
  }

  const report = [];
  for (const family of neededFamilies) {
    const isAvailable = !!availableFamilies[family];
    // Find closest available fallback
    let suggestedFallback = null;
    if (!isAvailable) {
      suggestedFallback = findFallbackFamily(family);
    }
    report.push({
      family,
      available: isAvailable,
      availableStyles: isAvailable ? [...availableFamilies[family]] : [],
      suggestedFallback,
    });
  }

  return report.sort((a, b) => {
    // Available fonts first, then missing
    if (a.available && !b.available) return -1;
    if (!a.available && b.available) return 1;
    return a.family.localeCompare(b.family);
  });
}

// Find a reasonable fallback for a missing font family
function findFallbackFamily(family) {
  const lower = family.toLowerCase();

  // Common system/Google font fallbacks in order of preference
  const FALLBACK_GROUPS = [
    // Sans-serif families
    { keywords: ['roboto', 'inter', 'helvetica', 'arial', 'nunito', 'poppins', 'lato', 'open sans', 'source sans', 'ubuntu', 'noto sans', 'oxygen', 'raleway', 'montserrat', 'work sans', 'barlow', 'mulish', 'dm sans', 'plus jakarta'],
      candidates: ['Inter', 'Roboto', 'Open Sans', 'Lato', 'Noto Sans'] },
    // Serif families
    { keywords: ['georgia', 'times', 'serif', 'playfair', 'merriweather', 'lora', 'source serif', 'noto serif', 'pt serif', 'libre baskerville', 'crimson'],
      candidates: ['Georgia', 'Times New Roman', 'Merriweather', 'Lora'] },
    // Monospace families
    { keywords: ['mono', 'code', 'courier', 'consolas', 'fira', 'source code', 'inconsolata', 'jetbrains', 'cascadia'],
      candidates: ['Courier New', 'Roboto Mono', 'Source Code Pro'] },
  ];

  for (const group of FALLBACK_GROUPS) {
    for (const kw of group.keywords) {
      if (lower.includes(kw)) {
        // Return first available candidate
        for (const candidate of group.candidates) {
          if (availableFamilies[candidate]) return candidate;
        }
      }
    }
  }

  // Default fallback
  for (const fallback of ['Inter', 'Roboto', 'Open Sans', 'Arial', 'Helvetica Neue']) {
    if (availableFamilies[fallback]) return fallback;
  }
  return 'Inter'; // Final fallback — Figma always has Inter
}

// ─── Color Variables ─────────────────────────────────────────
async function createColorVariables(colors) {
  if (!colors.length) return;

  colorVariableCollection = figma.variables.createVariableCollection('Imported Colors');

  let index = 0;
  for (const c of colors.slice(0, 50)) {
    try {
      const hex = c.hex;
      const name = `color-${index++}/${hex.replace('#', '')}`;
      const variable = figma.variables.createVariable(name, colorVariableCollection, 'COLOR');

      const r = parseInt(hex.slice(1, 3), 16) / 255;
      const g = parseInt(hex.slice(3, 5), 16) / 255;
      const b = parseInt(hex.slice(5, 7), 16) / 255;

      const modeId = colorVariableCollection.modes[0].modeId;
      variable.setValueForMode(modeId, { r, g, b, a: 1 });

      colorVariables[hex] = variable;
      stats.colors++;
    } catch (e) {
      console.warn('Failed to create color variable:', e);
    }
  }
}

// ─── Text Styles ─────────────────────────────────────────────
async function createTextStyles(fonts) {
  if (!fonts.length) return;

  for (const f of fonts.slice(0, 30)) {
    try {
      const key = `${f.family}|${f.weight}|${f.size}|${f.lineHeight}`;
      const styleName = `${f.family} / ${weightName(f.weight)} / ${Math.round(f.size)}`;

      const style = figma.createTextStyle();
      style.name = styleName;

      const resolved = await resolveFont(f.family, f.weight);
      try {
        await figma.loadFontAsync(resolved);
        style.fontName = resolved;
        stats.fontsLoaded++;
      } catch (e) {
        // Fallback to Inter
        const fallback = { family: 'Inter', style: weightToStyle(f.weight) };
        try {
          await figma.loadFontAsync(fallback);
          style.fontName = fallback;
        } catch (e2) {
          await figma.loadFontAsync({ family: 'Inter', style: 'Regular' });
          style.fontName = { family: 'Inter', style: 'Regular' };
        }
      }

      style.fontSize = f.size;
      if (f.lineHeight && f.lineHeight > 0) {
        style.lineHeight = { value: f.lineHeight, unit: 'PIXELS' };
      }

      textStylesMap[key] = style;
      stats.textStyles++;
    } catch (e) {
      console.warn('Failed to create text style:', e);
    }
  }
}

// ─── Font Resolution ─────────────────────────────────────────

/**
 * Given a CSS font-family name and weight, return the best matching
 * {family, style} available in Figma. Tracks misses for the report.
 */
async function resolveFont(family, weight) {
  const desiredStyle = weightToStyle(weight);

  // 1. Exact match
  if (availableFontsMap[`${family}|${desiredStyle}`]) {
    return { family, style: desiredStyle };
  }

  // 2. Family exists but style is missing — find closest weight
  if (availableFamilies[family]) {
    const closest = findClosestStyle(availableFamilies[family], weight);
    if (closest) return { family, style: closest };
  }

  // 3. Family not available — record as missing and use fallback
  const missKey = `${family}|${desiredStyle}`;
  if (!missingFonts[missKey]) {
    missingFonts[missKey] = { family, style: desiredStyle, usedBy: [] };
    stats.fontsMissing++;
  }

  const fallbackFamily = findFallbackFamily(family);
  const fallbackStyle = findClosestStyle(availableFamilies[fallbackFamily] || new Set(['Regular']), weight);
  return { family: fallbackFamily, style: fallbackStyle || 'Regular' };
}

/**
 * Find the closest available font style to a given weight number.
 */
function findClosestStyle(stylesSet, targetWeight) {
  if (!stylesSet || stylesSet.size === 0) return 'Regular';

  const styles = [...stylesSet];

  // Try exact style name first
  const exact = weightToStyle(targetWeight);
  if (styles.includes(exact)) return exact;

  // Try all numeric approximations
  const WEIGHT_MAP = {
    100: ['Thin', 'Extra Light', 'ExtraLight'],
    200: ['Extra Light', 'ExtraLight', 'Thin', 'Light'],
    300: ['Light', 'Extra Light', 'ExtraLight', 'Regular'],
    400: ['Regular', 'Book', 'Normal'],
    500: ['Medium', 'Regular'],
    600: ['Semi Bold', 'SemiBold', 'Demi Bold', 'DemiBold', 'Bold'],
    700: ['Bold', 'Semi Bold', 'SemiBold'],
    800: ['Extra Bold', 'ExtraBold', 'Black', 'Bold'],
    900: ['Black', 'Heavy', 'Extra Bold', 'ExtraBold'],
  };

  const candidates = WEIGHT_MAP[targetWeight] || WEIGHT_MAP[400];
  for (const candidate of candidates) {
    if (styles.includes(candidate)) return candidate;
  }

  // Return first available style as last resort
  return styles[0];
}

function weightName(w) {
  const map = {
    100: 'Thin', 200: 'ExtraLight', 300: 'Light', 400: 'Regular',
    500: 'Medium', 600: 'SemiBold', 700: 'Bold', 800: 'ExtraBold', 900: 'Black'
  };
  return map[w] || 'Regular';
}

function weightToStyle(w) {
  const map = {
    100: 'Thin', 200: 'Extra Light', 300: 'Light', 400: 'Regular',
    500: 'Medium', 600: 'Semi Bold', 700: 'Bold', 800: 'Extra Bold', 900: 'Black'
  };
  return map[w] || 'Regular';
}

// ─── Node Builder ────────────────────────────────────────────
async function buildNode(data, parent) {
  if (!data) return null;

  switch (data.type) {
    case 'TEXT':
      return await buildTextNode(data);
    case 'IMAGE':
      return await buildImageNode(data);
    case 'VECTOR':
      return buildVectorNode(data);
    case 'FRAME':
    default:
      return await buildFrameNode(data);
  }
}

// ─── Frame Node ──────────────────────────────────────────────
async function buildFrameNode(data) {
  const frame = figma.createFrame();
  stats.frames++;

  frame.name = cleanName(data.name, data.tag);

  frame.resize(
    Math.max(1, Math.round(data.width)),
    Math.max(1, Math.round(data.height))
  );

  // Background
  if (data.backgroundColor && data.backgroundColor.a > 0) {
    const fill = {
      type: 'SOLID',
      color: { r: data.backgroundColor.r, g: data.backgroundColor.g, b: data.backgroundColor.b },
      opacity: data.backgroundColor.a,
    };
    frame.fills = [fill];

    const hex = rgbToHex(data.backgroundColor.r, data.backgroundColor.g, data.backgroundColor.b);
    if (colorVariables[hex]) {
      try {
        const fillsCopy = clone(frame.fills);
        frame.fills = fillsCopy;
        figma.variables.setBoundVariableForPaint(fillsCopy[0], 'color', colorVariables[hex]);
        frame.fills = fillsCopy;
      } catch (e) { /* variable binding is best-effort */ }
    }
  } else {
    frame.fills = [];
  }

  // Border
  if (data.border) {
    const bw = data.border.width;
    const maxBorder = Math.max(bw.top, bw.right, bw.bottom, bw.left);
    if (maxBorder > 0 && data.border.color) {
      frame.strokes = [{
        type: 'SOLID',
        color: { r: data.border.color.r, g: data.border.color.g, b: data.border.color.b },
        opacity: data.border.color.a || 1,
      }];
      frame.strokeWeight = maxBorder;
      frame.strokeAlign = 'INSIDE';

      if (bw.top !== bw.right || bw.top !== bw.bottom || bw.top !== bw.left) {
        frame.strokeTopWeight = bw.top;
        frame.strokeRightWeight = bw.right;
        frame.strokeBottomWeight = bw.bottom;
        frame.strokeLeftWeight = bw.left;
      }
    }

    const br = data.border.radius;
    if (br && br.some(v => v > 0)) {
      frame.topLeftRadius = br[0];
      frame.topRightRadius = br[1];
      frame.bottomRightRadius = br[2];
      frame.bottomLeftRadius = br[3];
    }
  }

  // Opacity
  if (data.opacity !== undefined && data.opacity < 1) {
    frame.opacity = data.opacity;
  }

  // Clip content
  frame.clipsContent = !!data.clipContent;

  // ─── Auto Layout ───────────────────────────────────────
  if (data.layoutMode !== 'NONE' && data.autoLayout) {
    const al = data.autoLayout;

    if (al.direction === 'HORIZONTAL' || al.direction === 'VERTICAL') {
      frame.layoutMode = al.direction;
      frame.itemSpacing = al.gap || 0;

      if (data.padding) {
        frame.paddingTop = Math.round(data.padding.top);
        frame.paddingRight = Math.round(data.padding.right);
        frame.paddingBottom = Math.round(data.padding.bottom);
        frame.paddingLeft = Math.round(data.padding.left);
      }

      frame.primaryAxisAlignItems = mapJustifyContent(al.justifyContent);
      frame.counterAxisAlignItems = mapAlignItems(al.alignItems);
      frame.primaryAxisSizingMode = 'FIXED';
      frame.counterAxisSizingMode = 'FIXED';

      if (al.flexWrap === 'wrap' || al.flexWrap === 'wrap-reverse') {
        frame.layoutWrap = 'WRAP';
      }
    }
  }

  // ─── Box Shadow → Drop Shadow effect ──────────────────
  if (data.boxShadow) {
    const shadow = parseBoxShadow(data.boxShadow);
    if (shadow) {
      frame.effects = [{
        type: 'DROP_SHADOW',
        color: shadow.color,
        offset: { x: shadow.x, y: shadow.y },
        radius: shadow.blur,
        spread: shadow.spread,
        visible: true,
        blendMode: 'NORMAL',
      }];
    }
  }

  // ─── Build Children ────────────────────────────────────
  if (data.children && data.children.length > 0) {
    for (const childData of data.children) {
      const childNode = await buildNode(childData, frame);
      if (childNode) {
        frame.appendChild(childNode);

        if (frame.layoutMode !== 'NONE') {
          if (childData.flexGrow > 0) {
            childNode.layoutGrow = 1;
          }
          if (childData.alignSelf === 'stretch') {
            childNode.layoutAlign = 'STRETCH';
          }
          if (childData.position === 'absolute' || childData.position === 'fixed') {
            childNode.layoutPositioning = 'ABSOLUTE';
          }
        }
      }
    }
  }

  return frame;
}

// ─── Text Node ───────────────────────────────────────────────
async function buildTextNode(data) {
  if (!data.text || !data.text.characters) return null;

  const text = figma.createText();
  stats.texts++;

  text.name = cleanName(data.name, 'text');

  const fontFamily = data.text.fontFamily || 'Inter';
  const fontWeight = data.text.fontWeight || 400;

  // Resolve font (uses availability map + fallbacks)
  const resolvedFont = await resolveFont(fontFamily, fontWeight);
  try {
    await figma.loadFontAsync(resolvedFont);
    text.fontName = resolvedFont;
  } catch (e) {
    // Last resort
    await figma.loadFontAsync({ family: 'Inter', style: 'Regular' });
    text.fontName = { family: 'Inter', style: 'Regular' };
  }

  text.characters = data.text.characters;
  text.fontSize = data.text.fontSize || 16;

  if (data.text.lineHeight && data.text.lineHeight > 0) {
    text.lineHeight = { value: data.text.lineHeight, unit: 'PIXELS' };
  }

  if (data.text.letterSpacing && data.text.letterSpacing !== 0) {
    text.letterSpacing = { value: data.text.letterSpacing, unit: 'PIXELS' };
  }

  text.textAlignHorizontal = mapTextAlign(data.text.textAlign);

  if (data.text.color) {
    const fill = {
      type: 'SOLID',
      color: { r: data.text.color.r, g: data.text.color.g, b: data.text.color.b },
      opacity: data.text.color.a,
    };
    text.fills = [fill];

    const hex = rgbToHex(data.text.color.r, data.text.color.g, data.text.color.b);
    if (colorVariables[hex]) {
      try {
        const fillsCopy = clone(text.fills);
        figma.variables.setBoundVariableForPaint(fillsCopy[0], 'color', colorVariables[hex]);
        text.fills = fillsCopy;
      } catch (e) { /* best effort */ }
    }
  }

  if (data.text.textDecoration === 'underline') {
    text.textDecoration = 'UNDERLINE';
  } else if (data.text.textDecoration === 'line-through') {
    text.textDecoration = 'STRIKETHROUGH';
  }

  if (data.text.textTransform === 'uppercase') {
    text.textCase = 'UPPER';
  } else if (data.text.textTransform === 'lowercase') {
    text.textCase = 'LOWER';
  } else if (data.text.textTransform === 'capitalize') {
    text.textCase = 'TITLE';
  }

  // Apply text style if one matches
  const styleKey = `${fontFamily}|${fontWeight}|${data.text.fontSize}|${data.text.lineHeight}`;
  if (textStylesMap[styleKey]) {
    try {
      text.textStyleId = textStylesMap[styleKey].id;
    } catch (e) { /* best effort */ }
  }

  if (data.width > 0) {
    text.resize(Math.max(1, Math.round(data.width)), Math.max(1, Math.round(data.height || 20)));
    text.textAutoResize = 'HEIGHT';
  }

  return text;
}

// ─── Image Node ──────────────────────────────────────────────
async function buildImageNode(data) {
  const rect = figma.createRectangle();
  stats.images++;

  rect.name = cleanName(data.name, 'image');
  rect.resize(
    Math.max(1, Math.round(data.width)),
    Math.max(1, Math.round(data.height))
  );

  if (data.border && data.border.radius) {
    const br = data.border.radius;
    rect.topLeftRadius = br[0];
    rect.topRightRadius = br[1];
    rect.bottomRightRadius = br[2];
    rect.bottomLeftRadius = br[3];
  }

  if (data.imageSrc) {
    try {
      let imgUrl = data.imageSrc;
      if (imgUrl.startsWith('//')) {
        imgUrl = 'https:' + imgUrl;
      }

      if (imgUrl.startsWith('http')) {
        const image = await figma.createImageAsync(imgUrl);
        rect.fills = [{
          type: 'IMAGE',
          imageHash: image.hash,
          scaleMode: 'FILL',
        }];
      } else {
        rect.fills = [{ type: 'SOLID', color: { r: 0.85, g: 0.85, b: 0.85 } }];
      }
    } catch (e) {
      rect.fills = [{ type: 'SOLID', color: { r: 0.85, g: 0.85, b: 0.85 } }];
      console.warn('Failed to load image:', data.imageSrc, e);
    }
  }

  return rect;
}

// ─── Vector/SVG Node ─────────────────────────────────────────
function buildVectorNode(data) {
  const rect = figma.createRectangle();
  stats.vectors++;

  rect.name = cleanName(data.name, 'svg');
  rect.resize(
    Math.max(1, Math.round(data.width)),
    Math.max(1, Math.round(data.height))
  );

  // Light purple fill to indicate SVG placeholder
  rect.fills = [{ type: 'SOLID', color: { r: 0.9, g: 0.85, b: 1.0 }, opacity: 0.5 }];

  return rect;
}

// ─── Utility Functions ───────────────────────────────────────

function clone(val) {
  return JSON.parse(JSON.stringify(val));
}

function cleanName(name, tag) {
  if (!name || name === tag || name.length > 60) {
    return tag || 'layer';
  }
  return name.replace(/^[\.\s]+/, '').replace(/\s+/g, ' ').trim() || tag || 'layer';
}

function rgbToHex(r, g, b) {
  const toHex = (n) => {
    const hex = Math.round(n * 255).toString(16);
    return hex.length === 1 ? '0' + hex : hex;
  };
  return '#' + toHex(r) + toHex(g) + toHex(b);
}

function mapJustifyContent(value) {
  switch (value) {
    case 'center': return 'CENTER';
    case 'flex-end':
    case 'end': return 'MAX';
    case 'space-between': return 'SPACE_BETWEEN';
    case 'flex-start':
    case 'start':
    default: return 'MIN';
  }
}

function mapAlignItems(value) {
  switch (value) {
    case 'center': return 'CENTER';
    case 'flex-end':
    case 'end': return 'MAX';
    case 'baseline': return 'BASELINE';
    case 'flex-start':
    case 'start':
    default: return 'MIN';
  }
}

function mapTextAlign(value) {
  switch (value) {
    case 'center': return 'CENTER';
    case 'right': return 'RIGHT';
    case 'justify': return 'JUSTIFIED';
    case 'left':
    default: return 'LEFT';
  }
}

function parseBoxShadow(str) {
  if (!str || str === 'none') return null;

  const match = str.match(
    /(?:inset\s+)?(-?[\d.]+)px\s+(-?[\d.]+)px\s+([\d.]+)px\s*(?:([\d.]+)px)?\s*(rgba?\([^)]+\))?/
  );
  if (!match) return null;

  const x = parseFloat(match[1]) || 0;
  const y = parseFloat(match[2]) || 0;
  const blur = parseFloat(match[3]) || 0;
  const spread = parseFloat(match[4]) || 0;

  let color = { r: 0, g: 0, b: 0, a: 0.25 };
  if (match[5]) {
    const cm = match[5].match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/);
    if (cm) {
      color = {
        r: parseInt(cm[1]) / 255,
        g: parseInt(cm[2]) / 255,
        b: parseInt(cm[3]) / 255,
        a: cm[4] !== undefined ? parseFloat(cm[4]) : 1,
      };
    }
  }

  return { x, y, blur, spread, color };
}
