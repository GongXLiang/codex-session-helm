# Artwork sources

The hero is a generated still life, not a product screenshot. `hero-prompt.txt` is the exact prompt used with the built-in GPT image-generation tool. `hero-plate.png` is the unmodified generated image. Both language versions share this plate; their titles are laid out separately.

The four dependency illustrations use the task IDs, versions, statuses, and edges in `../../../examples/demo-state.json`. Each SVG connection records `data-from`, `data-to`, and `data-upstream-accepted` for inspection. Illustrations are static; update them if the example state changes.

Each SVG has a matching HTML export wrapper containing the same markup. After editing an SVG, replace the SVG element in its HTML wrapper before exporting. Open a wrapper in a browser to inspect it. The hero SVGs refer to `hero-plate.png` in this directory and are intended as source files, not remote README embeds.

Export sizes:

| Source | Browser viewport | Device scale | Published file |
| --- | --- | --- | --- |
| `hero-en.html`, `hero-zh.html` | 1536 × 512 | 1 | JPEG, quality 90 |
| `flow-wide-en.html`, `flow-wide-zh.html` | 880 × 526 | 2 | PNG, 1760 × 1052 |
| `flow-narrow-en.html`, `flow-narrow-zh.html` | 360 × 548 | 2 | PNG, 720 × 1096 |

Exports were made with Edge on Windows using Segoe UI and Noto Sans SC / Microsoft YaHei. Fonts have fallbacks; other systems may produce different text metrics. Inspect exported text and connector clearance before replacing the published assets.
