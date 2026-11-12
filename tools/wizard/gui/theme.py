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
    QPushButton#primary:disabled {{ background: #1b2a25; color: #5f7a70;
                                    border: 1px solid #22332d; }}
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


def modern_stylesheet() -> str:
    """Operator-first theme: large type, quiet surfaces, one accent action."""
    return f"""
    QWidget {{ background: #0d1014; color: {TEXT};
               font-family: 'Inter', 'Noto Sans', 'DejaVu Sans', sans-serif;
               font-size: 13px; }}
    QMainWindow, QDialog {{ background: #0d1014; }}
    QLabel {{ background: transparent; }}
    QFrame#topbar {{ background: transparent; border: none; }}
    QFrame#card, QFrame#panel {{ background: #141920; border: 1px solid #232c37;
                                  border-radius: 16px; }}
    QLabel#wordmark {{ font-size: 18px; font-weight: 800; letter-spacing: 3px;
                       color: #f2f6fb; }}
    QLabel#kicker {{ font-size: 10px; font-weight: 700; letter-spacing: 2px;
                     color: #7f8da0; }}
    QLabel#headline {{ color: #f2f6fb; }}
    QLabel#muted {{ color: #8c99ab; }}
    QLabel#mono {{ color: #aeb9c8; font-family: 'DejaVu Sans Mono'; font-size: 12px; }}
    QLabel#statusPill {{ color: {OK}; font-size: 11px; font-weight: 700;
                         letter-spacing: 1px; background: #10221c;
                         border: 1px solid #1f4d3c; border-radius: 12px;
                         padding: 5px 12px; }}
    QLabel#pill {{ color: #8c99ab; font-size: 10px; font-weight: 700;
                   letter-spacing: 1px; border: 1px solid #2a3442;
                   border-radius: 10px; padding: 3px 9px; }}
    QLabel#progressNumber {{ font-size: 30px; font-weight: 800; color: #f2f6fb; }}
    QLabel#chip {{ color: #7f8da0; background: #11161c; border: 1px solid #232c37;
                   border-radius: 9px; padding: 4px 9px; font-size: 11px; }}
    QLabel#chip[ok="true"] {{ color: #06251a; background: {OK};
                              border-color: {OK}; font-weight: 700; }}
    QLabel#feedback {{ color: {OK}; }}
    QLabel#errorBanner {{ color: #ffd7d7; background: #3a1518;
                          border: 1px solid #7f2a32; border-radius: 10px;
                          padding: 8px 12px; }}
    QPushButton {{ background: #1b222b; border: 1px solid #2b3644;
                   border-radius: 11px; padding: 10px 18px; color: #dce5f0;
                   font-weight: 600; }}
    QPushButton:hover {{ background: #222b36; border-color: #3b4a5d; }}
    QPushButton:disabled {{ color: #5b6675; background: #141920;
                            border-color: #1e2630; }}
    QPushButton#primary {{ background: {OK}; color: #052018; border: none;
                           font-size: 14px; font-weight: 800; padding: 12px 22px; }}
    QPushButton#primary:hover {{ background: #45d79e; }}
    QPushButton#primary:disabled {{ background: #1b2a25; color: #5f7a70;
                                    border: 1px solid #22332d; }}
    QPushButton#secondary {{ background: #1b222b; border-color: #334153; }}
    QPushButton#ghost {{ background: transparent; border: none; color: #8c99ab;
                         padding: 8px 10px; }}
    QPushButton#ghost:hover {{ color: #dce5f0; background: #141920; }}
    QFrame#overlay {{ background: rgba(5, 8, 12, 220); border-radius: 16px; }}
    QProgressBar {{ background: #1b222b; border: none; border-radius: 5px;
                    height: 10px; }}
    QProgressBar::chunk {{ background: {OK}; border-radius: 5px; }}
    QTabWidget::pane {{ border: 1px solid #232c37; border-radius: 10px; }}
    QTabBar::tab {{ background: #141920; color: #8c99ab; padding: 8px 14px;
                    border: none; }}
    QTabBar::tab:selected {{ color: #f2f6fb; border-bottom: 2px solid {OK}; }}
    QPlainTextEdit, QTextEdit, QListWidget {{ background: #0f1318;
                    border: 1px solid #232c37; border-radius: 10px;
                    color: #cbd5e1; font-family: 'DejaVu Sans Mono'; font-size: 12px; }}
    QScrollArea {{ border: none; }}
    """
