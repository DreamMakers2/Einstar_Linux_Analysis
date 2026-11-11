"""Qt6 (PySide6) calibration wizard — small theme helpers.

Dark, high-contrast, readable at arm's length on a laptop next to a scanner.
No external assets: everything is drawn.
"""
from __future__ import annotations

from PySide6 import QtGui

# palette
BG = "#12151a"
PANEL = "#1b2028"
PANEL_HI = "#232a34"
BORDER = "#2e3742"
TEXT = "#e7edf5"
MUTED = "#94a3b8"
OK = "#31c48d"
WARN = "#f0b429"
BAD = "#ef4444"
INFO = "#60a5fa"
ACCENT = "#8b5cf6"

VERDICT_COLOURS = {
    "constrained": OK,
    "weak": WARN,
    "unobservable": BAD,
    "unknown": MUTED,
}


def app_stylesheet() -> str:
    return f"""
    QWidget {{ background: {BG}; color: {TEXT};
               font-family: 'Noto Sans', 'DejaVu Sans', sans-serif; font-size: 13px; }}
    QFrame#panel {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; }}
    QLabel#title {{ font-size: 12px; color: {MUTED}; letter-spacing: 1px; }}
    QLabel#big {{ font-size: 26px; font-weight: 600; }}
    QLabel#huge {{ font-size: 34px; font-weight: 700; }}
    QLabel#value {{ font-size: 15px; font-weight: 600; }}
    QLabel#muted {{ color: {MUTED}; }}
    QPushButton {{ background: {PANEL_HI}; border: 1px solid {BORDER};
                   border-radius: 6px; padding: 8px 14px; }}
    QPushButton:hover {{ background: #2b3441; }}
    QPushButton:disabled {{ color: #5b6675; background: #1a1f27; }}
    QPushButton#primary {{ background: {OK}; color: #06251a; font-weight: 700; border: none; }}
    QPushButton#primary:hover {{ background: #45d79e; }}
    QPushButton#danger {{ background: {BAD}; color: #2a0707; font-weight: 700; border: none; }}
    QPushButton#warn {{ background: {WARN}; color: #2a1f04; font-weight: 700; border: none; }}
    QProgressBar {{ background: {PANEL_HI}; border: none; border-radius: 4px; height: 8px;
                    text-align: center; color: {MUTED}; font-size: 10px; }}
    QProgressBar::chunk {{ background: {OK}; border-radius: 4px; }}
    QListWidget {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; }}
    QListWidget::item {{ padding: 3px 6px; border-bottom: 1px solid #232a34; }}
    QScrollArea {{ border: none; }}
    QStatusBar {{ background: {PANEL}; color: {MUTED}; }}
    """


def mono_font(size: int = 12) -> QtGui.QFont:
    font = QtGui.QFont("DejaVu Sans Mono")
    font.setStyleHint(QtGui.QFont.StyleHint.Monospace)
    font.setPointSize(size)
    return font
