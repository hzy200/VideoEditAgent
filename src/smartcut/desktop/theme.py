"""主题、UI 缩放与高对比度。"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


_DARK = {
    "window": "#0b0f16",
    "surface": "#111723",
    "surface2": "#182131",
    "input": "#0d131c",
    "border": "#263143",
    "text": "#e5edf7",
    "muted": "#93a4b8",
    "accent": "#3b82f6",
    "accent2": "#2563eb",
}

_LIGHT = {
    "window": "#eef2f7",
    "surface": "#ffffff",
    "surface2": "#f5f8fc",
    "input": "#fbfcfe",
    "border": "#d4dde8",
    "text": "#172033",
    "muted": "#5f6f85",
    "accent": "#2563eb",
    "accent2": "#1d4ed8",
}


def _qss(c: dict[str, str], contrast: bool) -> str:
    base = f"""
QWidget {{ color: {c["text"]}; }}
QMainWindow, QDialog {{ background: {c["window"]}; }}
QToolTip {{ background: {c["surface"]}; color: {c["text"]};
           border: 1px solid {c["border"]}; padding: 4px 6px; }}

QMenuBar {{ background: {c["surface"]}; border-bottom: 1px solid {c["border"]};
            padding: 2px 6px; }}
QMenuBar::item {{ padding: 5px 10px; border-radius: 4px; background: transparent; }}
QMenuBar::item:selected {{ background: {c["surface2"]}; }}
QMenu {{ background: {c["surface"]}; border: 1px solid {c["border"]}; padding: 4px; }}
QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 4px; }}
QMenu::item:selected {{ background: {c["accent"]}; color: #ffffff; }}
QMenu::separator {{ height: 1px; background: {c["border"]}; margin: 4px 8px; }}

QToolBar {{ background: {c["surface"]}; border: 0;
            border-bottom: 1px solid {c["border"]}; spacing: 4px; padding: 4px 8px; }}
QToolBar::separator {{ width: 1px; background: {c["border"]}; margin: 6px; }}
QToolButton {{ background: transparent; border: 1px solid transparent;
               border-radius: 6px; padding: 5px 8px; color: {c["text"]}; }}
QToolButton:hover {{ background: {c["surface2"]}; border-color: {c["border"]}; }}
QToolButton:pressed, QToolButton:checked {{
    background: {c["accent"]}; color: #ffffff; }}

QStatusBar {{ background: {c["surface"]}; border-top: 1px solid {c["border"]};
              color: {c["muted"]}; }}
QStatusBar::item {{ border: none; }}

QSplitter::handle {{ background: {c["border"]}; }}
QSplitter::handle:horizontal {{ width: 2px; }}
QSplitter::handle:vertical {{ height: 2px; }}

QTabWidget::pane {{ border: 1px solid {c["border"]}; border-radius: 8px;
                    background: {c["surface"]}; top: -1px; }}
QTabBar::tab {{ background: transparent; color: {c["muted"]}; padding: 8px 14px;
                margin-right: 2px; border-bottom: 2px solid transparent; }}
QTabBar::tab:hover {{ color: {c["text"]}; background: {c["surface2"]}; }}
QTabBar::tab:selected {{ color: {c["text"]}; border-bottom-color: {c["accent"]};
                         background: {c["surface2"]}; }}

QGroupBox {{ background: {c["surface"]}; border: 1px solid {c["border"]};
             border-radius: 8px; margin-top: 14px; padding: 10px 8px 8px 8px;
             font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left;
                    left: 10px; padding: 0 6px; color: {c["muted"]}; }}

QPushButton {{ background: {c["surface2"]}; border: 1px solid {c["border"]};
               border-radius: 6px; padding: 6px 12px; min-height: 18px; }}
QPushButton:hover {{ border-color: {c["accent"]}; }}
QPushButton:pressed {{ background: {c["accent"]}; color: #ffffff; }}
QPushButton:disabled {{ color: {c["muted"]}; background: {c["window"]}; }}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {c["input"]}; border: 1px solid {c["border"]}; border-radius: 6px;
    padding: 5px 8px; selection-background-color: {c["accent"]};
    selection-color: #ffffff; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {c["accent"]}; }}
QComboBox::drop-down {{ border: 0; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {c["surface"]}; border: 1px solid {c["border"]};
    selection-background-color: {c["accent"]}; selection-color: #ffffff; outline: 0; }}

QTableWidget, QListWidget, QTextBrowser, QPlainTextEdit, QTextEdit {{
    background: {c["input"]}; border: 1px solid {c["border"]}; border-radius: 6px;
    alternate-background-color: {c["surface2"]}; gridline-color: {c["border"]};
    selection-background-color: {c["accent2"]}; selection-color: #ffffff; }}
QHeaderView::section {{ background: {c["surface2"]}; color: {c["muted"]};
    padding: 6px; border: 0; border-right: 1px solid {c["border"]};
    border-bottom: 1px solid {c["border"]}; }}
QTableCornerButton::section {{ background: {c["surface2"]}; border: 0; }}

QSlider::groove:horizontal {{ height: 4px; background: {c["border"]};
                              border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {c["accent"]}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: #ffffff; border: 2px solid {c["accent"]};
    width: 12px; height: 12px; margin: -5px 0; border-radius: 7px; }}

QProgressBar {{ background: {c["input"]}; border: 1px solid {c["border"]};
                border-radius: 4px; text-align: center; color: {c["muted"]}; }}
QProgressBar::chunk {{ background: {c["accent"]}; border-radius: 3px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {c["border"]}; border-radius: 4px;
                               min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {c["muted"]}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {c["border"]}; border-radius: 4px;
                                 min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QLabel#mutedText {{ color: {c["muted"]}; }}
QLabel#accentText {{ color: {c["accent"]}; }}
QLabel#videoPlaceholder {{ color: {c["muted"]}; font-size: 15px; }}
QFrame#videoFrame {{ background: #05070b; border: 1px solid {c["border"]};
                     border-radius: 10px; }}

QCheckBox {{ spacing: 6px; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid {c["border"]};
                        border-radius: 3px; background: {c["input"]}; }}
QCheckBox::indicator:checked {{ background: {c["accent"]}; border-color: {c["accent"]}; }}
"""
    if contrast:
        base += """
QPushButton { border: 2px solid #facc15; padding: 5px; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox,
QPlainTextEdit, QTextEdit, QTableWidget, QListWidget {
    border: 2px solid #facc15;
}
QGroupBox { border: 2px solid #facc15; }
QTabBar::tab:selected { border: 2px solid #facc15; border-bottom-color: #facc15; }
"""
    return base


def apply_theme(app: QApplication, theme: str, *, scale: float = 1.0, contrast: bool = False) -> None:
    base_size = app.property("smartcut_base_font_size")
    if base_size is None:
        base_size = app.font().pointSizeF() or 9.0
        app.setProperty("smartcut_base_font_size", base_size)
    font = app.font()
    font.setPointSizeF(max(6.0, float(base_size) * max(0.75, min(2.0, scale))))
    app.setFont(font)

    if theme == "light":
        palette = app.style().standardPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(_LIGHT["window"]))
        palette.setColor(QPalette.ColorRole.Base, QColor(_LIGHT["input"]))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor(_LIGHT["surface2"]))
        palette.setColor(QPalette.ColorRole.Text, QColor(_LIGHT["text"]))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(_LIGHT["text"]))
        palette.setColor(QPalette.ColorRole.Button, QColor(_LIGHT["surface2"]))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(_LIGHT["text"]))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(_LIGHT["accent"]))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        colors = _LIGHT
    else:
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(_DARK["window"]))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(_DARK["text"]))
        palette.setColor(QPalette.ColorRole.Base, QColor(_DARK["input"]))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor(_DARK["surface2"]))
        palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(_DARK["surface"]))
        palette.setColor(QPalette.ColorRole.ToolTipText, QColor(_DARK["text"]))
        palette.setColor(QPalette.ColorRole.Text, QColor(_DARK["text"]))
        palette.setColor(QPalette.ColorRole.Button, QColor(_DARK["surface2"]))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(_DARK["text"]))
        palette.setColor(QPalette.ColorRole.BrightText, QColor("#f87171"))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(_DARK["accent"]))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        colors = _DARK
    app.setPalette(palette)
    app.setStyleSheet(_qss(colors, contrast))
