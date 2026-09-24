import ast
import re
from pathlib import Path


GUI = Path(__file__).parents[1] / "src" / "actl" / "gui.py"


def _constants():
    tree = ast.parse(GUI.read_text())
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                values[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                if isinstance(node.value, ast.Name):
                    values[node.targets[0].id] = values[node.value.id]
    return values


def _contrast(foreground, background):
    def channel(value):
        value = int(value, 16) / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    def luminance(color):
        rgb = color.removeprefix("#")
        return 0.2126 * channel(rgb[0:2]) + 0.7152 * channel(rgb[2:4]) + 0.0722 * channel(rgb[4:6])

    light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_hermes_palette_and_contrast():
    colors = _constants()
    assert {name: colors[name] for name in ("BG", "PANEL", "PANEL2", "LINE", "TXT", "DIM", "ACC", "NEON", "OK", "WARN", "BAD")} == {
        "BG": "#0B0D10", "PANEL": "#12151A", "PANEL2": "#161A20", "LINE": "#262B33",
        "TXT": "#E6E8EC", "DIM": "#9AA3AE", "ACC": "#7FD4C1", "NEON": "#7FD4C1",
        "OK": "#7FD4C1", "WARN": "#E8C270", "BAD": "#E0726C",
    }
    for foreground in (colors["TXT"], colors["DIM"]):
        for background in (colors["BG"], colors["PANEL"]):
            assert _contrast(foreground, background) >= 4.5
    assert _contrast(colors["BG"], colors["ACC"]) >= 4.5


def test_theme_has_no_legacy_color_literals():
    source = GUI.read_text()
    assert not re.search(r"#(?:fff7ed|005bb5|fafafa)\b", source, re.IGNORECASE)
