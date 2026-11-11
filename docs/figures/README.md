# Figures

See also: [documentation map](../../README.md#documentation). Pages embedding these figures: [README](../../README.md) · [agent architecture](../agent-architecture.md) · [development](../development.md) · [evaluation methodology](../eval-methodology.md) · [MCP client guide](../mcp-client-guide.md) · [relay artefact](../relay-artefact.md) · [runbook](../runbook.md) · [security baseline](../security-baseline.md)

Each figure is a draw.io source (`<name>.drawio`) next to its SVG export (`<name>.svg`); the
markdown pages link the SVG. Edit the source, then re-export; never edit an SVG by hand.

- **Toolchain.** draw.io desktop 32.6.10 (`brew install --cask drawio`). Export with:

  ```bash
  DRAWIO=/Applications/draw.io.app/Contents/MacOS/draw.io
  "$DRAWIO" -x -f svg --embed-svg-images --theme light -o docs/figures/<name>.svg docs/figures/<name>.drawio
  ```

  `--theme light` keeps a fixed white background; without it the export adapts to the viewer's
  dark mode.
- **Plain-text sources.** Every `.drawio` is saved uncompressed (`compressed="false"`), and every
  label is plain text (`html=0`, no `whiteSpace=wrap`), so the SVG carries real `<text>` and no
  `<foreignObject>`. `scripts/check_doc_figures.py` checks the source format and the export.
- **Titles.** Every figure has a title cell, `id="figure-title"`, of the form
  `CRM Agent Eval — <what the figure shows>`. It is bold, font size 22, `align=center`, and 32 px
  tall. It sits 64 px above the topmost content cell and is centered on the exported SVG's width,
  so its text anchor is within 2 px of the viewBox center. Base the wording
  on the heading or alt text that introduces the figure, and don't let it contradict the alt text.
- **Icons.** AWS icons come from draw.io's bundled `aws4` shape library, which reproduces the AWS
  Architecture Icons. AWS permits the use of its Architecture Icons to create architecture diagrams.
