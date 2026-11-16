# Contributing to Anycubic Slicer MCP

Thanks for taking a look. This project lets AI agents drive Anycubic Slicer Next, and it improves with every printer someone tests and every fix someone sends. First contributions are welcome.

## Ways to help

| Time | Idea |
|---|---|
| 5 minutes | Fix a typo or an unclear sentence in the docs |
| 15 minutes | [Report how it works with your printer](https://github.com/pri-te-sh/anycubic-slicer-mcp/issues/new?template=printer_report.yml), even if everything worked |
| An evening | Fix a bug, add a test, add a print defect to the doctor or a material to the filament table |
| A weekend | Linux support, another slicer built on OrcaSlicer, better previews |

If you're not sure where to start, look for issues labelled [`good first issue`](https://github.com/pri-te-sh/anycubic-slicer-mcp/labels/good%20first%20issue) or open an issue describing what you'd like to do.

## Development setup

You need Python 3.10 or newer, or [uv](https://docs.astral.sh/uv/). The test suite doesn't need the slicer installed; the tests that do are skipped automatically.

macOS:

```bash
git clone https://github.com/<you>/anycubic-slicer-mcp
cd anycubic-slicer-mcp
uv run --python 3.13 --with-editable ".[test]" pytest
```

Windows (PowerShell):

```powershell
git clone https://github.com/<you>/anycubic-slicer-mcp
cd anycubic-slicer-mcp
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[test]"
.venv\Scripts\python.exe -m pytest
```

To try your changes in an MCP client, point the client at your checkout:

```bash
uv run --directory /path/to/anycubic-slicer-mcp anycubic-slicer-mcp
```

## Project layout

```
anycubic_slicer_mcp/
  server.py       MCP tools and prompts (thin wrappers with input checks)
  install.py      where the slicer and its data live on each OS
  presets.py      preset discovery, inheritance, the GUI's current selection
  settings.py     routes overrides to the right preset, blocks unsafe keys
  slicer.py       runs the slicer's command line, reads the results
  mesh.py         STL/OBJ/3MF loading, transforms, overhang and section analysis
  gcode.py        G-code summary and toolpath parsing
  render.py       previews, layer GIFs, print cards
  analysis.py     orientation ranking, strength advice, bed fit
  materials.py    material table and filament profiles from label data
  inventory.py    local spool inventory
  doctor.py       print defect catalogue and setting suggestions
  calibration.py  calibration models
  optimize.py     variant comparison, cost, multi-colour waste
tests/            offline tests, plus integration tests that run when the slicer is installed
examples/         the example gear and the build123d script that makes it
docs/             the website, its images and the scripts that generate them
```

## Guidelines

- **Keep it cross-platform.** OS-specific paths belong in `install.py`. Add a test that covers both Windows and macOS; the `Install` dataclass takes `os_name`, so both run anywhere.
- **Keep it safe.** Don't add a way to set `post_process`, run programs, reach the network, or write outside the output folder and the user's preset folder. If a feature needs one of those, open an issue first.
- **Add tests** for new behaviour. If you change slicing, also slice `examples/gear.stl` with a real slicer and mention the printer and OS in the PR.
- **Keep PRs small.** One change per PR is easiest to review. Update the README and the website when you add or change a tool.
- Match the existing style: type hints, short docstrings, and no new dependencies without a discussion.

## Regenerating the images

```bash
python3 docs/tools/make_art.py
rsvg-convert -w 1280 -h 640 docs/assets/hero-static.svg > docs/assets/social-preview.png
uv run --python 3.13 --with-editable . python docs/tools/make_previews.py
uv run --python 3.14 --with build123d python examples/make_gear.py
```

`make_previews.py` slices the example gear with your installed slicer, so the preview images and the numbers on the website come from a real run.

## Pull request checklist

- [ ] Tests pass locally
- [ ] New behaviour has tests
- [ ] README and website updated if tools or setup changed
- [ ] Tested with a real slicer if slicing changed (printer and OS noted in the PR)

## Code of conduct

Be kind, assume good intent and help newcomers. Harassment or discrimination of any kind isn't tolerated. Report problems by opening an issue or contacting the maintainer through GitHub.

## Trademarks

This is an independent community project. Please don't use Anycubic's logos or suggest endorsement in contributions. Naming Anycubic Slicer Next or a printer model to describe compatibility is fine.
