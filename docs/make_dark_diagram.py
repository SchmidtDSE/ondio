"""Write the dark-mode copy of each docs diagram, as <name>-dark.svg next to it.

Edit only the light diagrams. This script swaps each of their colors for a dark-mode
one, and stops if a light diagram uses a color that has no swap here, so that a
new color cannot slip into a dark diagram unchanged. The docs tasks run it
before every build.
"""

import re
import sys
from pathlib import Path

# Next to this script, not in content/: zensical publishes every file there.
CONTENT = Path(__file__).parent / "content"
LIGHTS = [CONTENT / "architecture.svg", CONTENT / "flac-format.svg"]

# light color -> dark color, grouped by what the color is for
SWAPS = {
    # canvas, text, and lines
    "#f6f8fb": "#161b22",  # background
    "#14304a": "#e6edf3",  # main text
    "#61758a": "#9aa8b6",  # secondary text and arrows
    "#becbd7": "#3a4654",  # connector lines
    # dispatcher
    "#f0ecfa": "#2a2342",  # fill
    "#7a5ec9": "#a08be0",  # accent bar
    "#4a2e83": "#c9b8ff",  # heading
    # registry
    "#e9f1fb": "#1a2a3d",  # fill
    "#1f4e79": "#8cc2f2",  # heading
    # format helpers
    "#eaf5ef": "#1b2e25",  # fill
    "#1f6b45": "#8fd6ad",  # heading
    # storage backends
    "#e6f7f3": "#15302b",  # fill
    "#0b6e5c": "#6fd8c0",  # heading
    "#14a48a": "#2fc4a6",  # rule
}

COLOR = re.compile(r"#[0-9a-fA-F]{6}\b")

for path in LIGHTS:
    light = path.read_text(encoding="utf-8")
    unknown = sorted({c.lower() for c in COLOR.findall(light)} - SWAPS.keys())
    if unknown:
        sys.exit(f"{path} uses colors with no dark swap in {__file__}: {', '.join(unknown)}")
    dark = path.with_name(f"{path.stem}-dark.svg")
    dark.write_text(COLOR.sub(lambda m: SWAPS[m.group().lower()], light), encoding="utf-8")
