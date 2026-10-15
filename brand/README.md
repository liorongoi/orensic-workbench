# Lucida brand assets

Lucida is named after the camera lucida, the prism that lets you see the scene and your drawing
at once. The mark is a split square: the filled half is the original, the outlined half the
processed result, joined by the lens.

| File | Use |
| --- | --- |
| `lucida-icon.svg` | Master icon. The UI favicon (`frontend/src/assets/lucida.svg`) and the inline logo in HTML reports (`backend/forensic/report.py`) are copies of it. |
| `lucida-icon-256.png` | Raster icon for places that cannot take SVG. |
| `../deploy/windows/lucida.ico` | Windows installer, Start menu and desktop shortcut icon (16 to 256 px). |

Colours: ink `#0D201C`, mint `#8FE3C8`. The wordmark is set in Instrument Serif Italic
(SIL Open Font License 1.1), bundled into the UI from `@fontsource/instrument-serif` so it
works offline; reports fall back to Georgia.

To regenerate the PNG and ICO after changing the SVG, render the SVG at 16, 24, 32, 48, 64,
128 and 256 px (any SVG renderer, for example a headless browser) and combine them with
ImageMagick: `convert icon-16.png ... icon-256.png deploy/windows/lucida.ico`.
