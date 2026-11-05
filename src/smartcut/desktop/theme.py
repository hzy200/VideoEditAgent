"""主题、UI 缩放与高对比度。

深色主题面向专业 NLE：深石墨背景、分层表面、青蓝强调色与高对比轨道。
所有颜色集中在本模块，QWidget 内不得再写死主题色（画布类自定义绘制除外）。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QApplication

_DARK = {
    "window": "#0a0e15",
    "window2": "#070a10",
    "surface": "#0f1622",
    "surface2": "#151f2e",
    "surface3": "#1c2839",
    "input": "#0b1119",
    "border": "#233044",
    "border_strong": "#33455f",
    "text": "#e7eef8",
    "muted": "#8fa1ba",
    "accent": "#38bdf8",
    "accent2": "#0ea5e9",
    "accent_deep": "#0284c7",
    "accent_lite": "#7dd3fc",
    "accent_soft": "rgba(56, 189, 248, 0.14)",
    "accent_softer": "rgba(56, 189, 248, 0.07)",
    "accent_border": "rgba(56, 189, 248, 0.42)",
    "on_accent": "#04121c",
    "success": "#34d399",
    "warning": "#fbbf24",
    "danger": "#fb7185",
    "danger_soft": "rgba(251, 113, 133, 0.14)",
    "danger_border": "rgba(251, 113, 133, 0.45)",
    "video_bg": "#05070b",
    "white_handle": "#f8fafc",
}

_LIGHT = {
    "window": "#eef2f8",
    "window2": "#e4ebf4",
    "surface": "#ffffff",
    "surface2": "#f4f7fc",
    "surface3": "#e8eff8",
    "input": "#fbfdff",
    "border": "#d7e1ee",
    "border_strong": "#b9c9dc",
    "text": "#162033",
    "muted": "#5c6f88",
    "accent": "#0284c7",
    "accent2": "#0369a1",
    "accent_deep": "#075985",
    "accent_lite": "#0ea5e9",
    "accent_soft": "rgba(2, 132, 199, 0.11)",
    "accent_softer": "rgba(2, 132, 199, 0.06)",
    "accent_border": "rgba(2, 132, 199, 0.38)",
    "on_accent": "#ffffff",
    "success": "#059669",
    "warning": "#b45309",
    "danger": "#e11d48",
    "danger_soft": "rgba(225, 29, 72, 0.09)",
    "danger_border": "rgba(225, 29, 72, 0.35)",
    "video_bg": "#0b0f16",
    "white_handle": "#ffffff",
}

# 时间线/预览画布使用独立的深色 NLE 调色板，不随浅色主题变化。
NLE = {
    "canvas": "#0c121c",
    "lane": "#090e16",
    "grid": "#223147",
    "text": "#8fa1ba",
    "muted": "#5b6d86",
    "white": "#f8fafc",
    "playhead": "#f43f5e",
    "selection": "#67e8f9",
    "badge": "#1b2637",
    "muted_track": "#475569",
    "solo": "#f59e0b",
    "clip_palette": (
        "#3b82f6",
        "#8b5cf6",
        "#06b6d4",
        "#d946ef",
        "#ec4899",
        "#14b8a6",
    ),
    "audio_palette": {
        "main": "#2563eb",
        "music": "#0d9488",
        "voice": "#7c3aed",
        "stem_vocals": "#db2777",
        "stem_instrumental": "#0891b2",
    },
}


def _qss(c: dict[str, str], contrast: bool) -> str:
    grad_surface = (
        f"qlineargradient(x1:0, y1:0, x2:0, y2:1, "
        f"stop:0 {c['surface2']}, stop:1 {c['surface']})"
    )
    grad_toolbar = (
        f"qlineargradient(x1:0, y1:0, x2:0, y2:1, "
        f"stop:0 {c['surface']}, stop:1 {c['window']})"
    )
    grad_primary = (
        f"qlineargradient(x1:0, y1:0, x2:0, y2:1, "
        f"stop:0 {c['accent']}, stop:1 {c['accent2']})"
    )
    grad_primary_hover = (
        f"qlineargradient(x1:0, y1:0, x2:0, y2:1, "
        f"stop:0 {c['accent_lite']}, stop:1 {c['accent']})"
    )
    base = f"""
QWidget {{ color: {c["text"]}; }}
QMainWindow, QDialog {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {c["window"]}, stop:1 {c["window2"]});
}}
QToolTip {{ background: {c["surface2"]}; color: {c["text"]};
           border: 1px solid {c["border_strong"]}; border-radius: 6px;
           padding: 5px 8px; }}

QMenuBar {{ background: {c["surface"]}; border-bottom: 1px solid {c["border"]};
            padding: 2px 8px; spacing: 2px; }}
QMenuBar::item {{ padding: 6px 11px; border-radius: 6px; background: transparent; }}
QMenuBar::item:selected {{ background: {c["accent_soft"]}; color: {c["accent"]}; }}
QMenu {{ background: {c["surface2"]}; border: 1px solid {c["border_strong"]};
         border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 28px 7px 13px; border-radius: 6px; }}
QMenu::item:selected {{ background: {grad_primary}; color: {c["on_accent"]}; }}
QMenu::item:disabled {{ color: {c["muted"]}; }}
QMenu::separator {{ height: 1px; background: {c["border"]}; margin: 5px 9px; }}

QToolBar {{ background: {grad_toolbar}; border: 0;
            border-bottom: 1px solid {c["border"]}; spacing: 5px;
            padding: 6px 10px; }}
QToolBar::separator {{ width: 1px; background: {c["border_strong"]}; margin: 5px 7px; }}
QToolButton {{ background: transparent; border: 1px solid transparent;
               border-radius: 7px; padding: 6px 10px; color: {c["text"]};
               font-weight: 500; }}
QToolButton:hover {{ background: {c["surface3"]}; border-color: {c["border_strong"]}; }}
QToolButton:pressed {{ background: {c["input"]}; border-color: {c["accent"]}; }}
QToolButton:checked {{ background: {grad_primary}; color: {c["on_accent"]};
                       border-color: {c["accent2"]}; }}
QToolButton#primaryToolButton {{
    background: {grad_primary}; color: {c["on_accent"]};
    border: 1px solid {c["accent2"]}; font-weight: 600; padding: 6px 14px; }}
QToolButton#primaryToolButton:hover {{ background: {grad_primary_hover};
    border-color: {c["accent_lite"]}; }}
QToolBar QToolButton {{ padding: 4px; }}
QToolBar QToolButton#primaryToolButton {{ padding: 6px 14px; }}
QToolButton#transportPlay {{ background: {grad_primary};
    border: 1px solid {c["accent2"]}; border-radius: 15px;
    padding: 3px; }}
QToolButton#transportPlay:hover {{ background: {grad_primary_hover};
    border-color: {c["accent_lite"]}; }}
QToolButton#transportPlay:checked {{ background: {c["accent2"]};
    border-color: {c["accent_lite"]}; }}
QToolButton#transportPlay:disabled {{ background: {c["surface2"]};
    border-color: {c["border"]}; }}
QFrame#toolbarSeparator {{ background: {c["border_strong"]};
    border: none; max-width: 1px; }}

QStatusBar {{ background: {c["surface"]}; border-top: 1px solid {c["border"]};
              color: {c["muted"]}; padding: 2px 6px; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {c["muted"]}; padding: 0 4px; }}

QSplitter::handle {{ background: transparent; }}
QSplitter::handle:hover {{ background: {c["accent_border"]}; }}
QSplitter::handle:horizontal {{ width: 4px; }}
QSplitter::handle:vertical {{ height: 4px; }}

QTabWidget::pane {{ border: 1px solid {c["border"]}; border-radius: 12px;
                    background: {c["surface"]}; top: -1px; padding: 2px; }}
QTabWidget#workspaceTabs::pane {{ border: 1px solid {c["border"]};
                    border-radius: 12px; background: {c["surface"]}; top: 0; }}
QTabWidget#panelTabs::pane {{ border: none; background: transparent; top: 0; padding: 0; }}
QTabBar#workspaceTabBar {{ qproperty-drawBase: 0; }}
QTabBar#workspaceTabBar::tab {{ background: transparent; color: {c["muted"]};
    padding: 9px 18px; margin: 4px 5px 2px 0; border-radius: 8px;
    border: 1px solid transparent; font-weight: 600; }}
QTabBar#workspaceTabBar::tab:hover {{ background: {c["surface2"]}; color: {c["text"]}; }}
QTabBar#workspaceTabBar::tab:selected {{ background: {c["accent_soft"]};
    color: {c["accent"]}; border-color: {c["accent_border"]}; }}
QTabBar#panelTabBar {{ qproperty-drawBase: 0; }}
QTabBar#panelTabBar::tab, QTabBar::tab {{ background: transparent;
    color: {c["muted"]}; padding: 7px 13px; margin-right: 2px;
    border-bottom: 2px solid transparent; }}
QTabBar#panelTabBar::tab:hover, QTabBar::tab:hover {{
    color: {c["text"]}; background: {c["accent_softer"]}; }}
QTabBar#panelTabBar::tab:selected, QTabBar::tab:selected {{
    color: {c["accent"]}; border-bottom-color: {c["accent"]}; }}

QGroupBox {{ background: {grad_surface}; border: 1px solid {c["border"]};
             border-radius: 10px; margin-top: 16px; padding: 14px 10px 10px 10px;
             font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left;
                    left: 12px; padding: 0 6px; color: {c["muted"]}; }}

QPushButton {{ background: {grad_surface}; border: 1px solid {c["border_strong"]};
               border-radius: 7px; padding: 7px 14px; min-height: 20px;
               font-weight: 500; }}
QPushButton:hover {{ background: {c["surface3"]}; border-color: {c["accent_border"]}; }}
QPushButton:pressed {{ background: {c["input"]}; border-color: {c["accent"]}; }}
QPushButton:focus {{ border-color: {c["accent"]}; }}
QPushButton:checked {{ background: {c["accent_soft"]}; color: {c["accent"]};
                       border-color: {c["accent_border"]}; }}
QPushButton:disabled {{ color: {c["muted"]}; background: {c["window2"]};
                        border-color: {c["border"]}; }}
QPushButton#primaryButton {{ background: {grad_primary};
    border: 1px solid {c["accent2"]}; color: {c["on_accent"]}; font-weight: 600; }}
QPushButton#primaryButton:hover {{ background: {grad_primary_hover};
    border-color: {c["accent_lite"]}; }}
QPushButton#primaryButton:disabled {{ background: {c["surface2"]};
    border-color: {c["border"]}; color: {c["muted"]}; }}
QPushButton#dangerButton {{ background: {c["danger_soft"]};
    border: 1px solid {c["danger_border"]}; color: {c["danger"]}; }}
QPushButton#dangerButton:hover {{ background: {c["danger"]}; color: {c["on_accent"]}; }}
QDialogButtonBox QPushButton {{ min-width: 78px; }}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {c["input"]}; border: 1px solid {c["border"]}; border-radius: 7px;
    padding: 6px 10px; selection-background-color: {c["accent"]};
    selection-color: {c["on_accent"]}; placeholder-text-color: {c["muted"]}; }}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{
    border-color: {c["border_strong"]}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {c["accent"]}; background: {c["surface"]}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color: {c["muted"]}; background: {c["window2"]}; }}
QComboBox {{ padding-right: 24px; }}
QComboBox::drop-down {{ border: 0; width: 24px; }}
QComboBox QAbstractItemView {{
    background: {c["surface2"]}; border: 1px solid {c["border_strong"]};
    border-radius: 8px; padding: 4px;
    selection-background-color: {c["accent_soft"]}; selection-color: {c["accent"]};
    outline: 0; }}

QTableWidget, QListWidget, QTreeWidget, QTextBrowser, QPlainTextEdit, QTextEdit {{
    background: {c["input"]}; border: 1px solid {c["border"]}; border-radius: 9px;
    alternate-background-color: {c["surface2"]}; gridline-color: {c["border"]};
    selection-background-color: {c["accent2"]}; selection-color: {c["on_accent"]};
    outline: 0; }}
QTableWidget::item {{ padding: 4px 6px; border: none; }}
QTableWidget::item:selected, QTreeWidget::item:selected {{
    background: {c["accent2"]}; color: {c["on_accent"]}; }}
QTableWidget::item:hover {{ background: {c["accent_softer"]}; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{ background: {c["surface2"]}; color: {c["muted"]};
    padding: 7px 8px; border: 0; border-right: 1px solid {c["border"]};
    border-bottom: 1px solid {c["border"]}; font-weight: 600; }}
QTableCornerButton::section {{ background: {c["surface2"]}; border: 0; }}
QListWidget {{ padding: 4px; }}
QListWidget::item {{ padding: 7px 9px; border-radius: 6px; margin: 1px 2px;
    border: 1px solid transparent; }}
QListWidget::item:hover {{ background: {c["surface2"]}; }}
QListWidget::item:selected {{ background: {c["accent_soft"]};
    color: {c["accent"]}; border-color: {c["accent_border"]}; }}

QSlider::groove:horizontal {{ height: 5px; background: {c["border"]};
                              border-radius: 2px; }}
QSlider::sub-page:horizontal {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {c["accent_deep"]}, stop:1 {c["accent"]});
    border-radius: 2px; }}
QSlider::add-page:horizontal {{ background: {c["border"]}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {c["white_handle"]};
    border: 2px solid {c["accent"]}; width: 13px; height: 13px;
    margin: -6px 0; border-radius: 8px; }}
QSlider::handle:horizontal:hover {{ border-color: {c["accent_lite"]};
    background: #ffffff; }}
QSlider::handle:horizontal:pressed {{ background: {c["accent_lite"]}; }}
QSlider::handle:horizontal:disabled {{ border-color: {c["border_strong"]};
    background: {c["surface2"]}; }}

QProgressBar {{ background: {c["input"]}; border: 1px solid {c["border"]};
                border-radius: 6px; min-height: 12px; max-height: 14px;
                text-align: center; color: {c["muted"]};
                font-size: 9px; }}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {c["accent2"]}, stop:1 {c["accent"]});
    border-radius: 5px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px 1px; }}
QScrollBar::handle:vertical {{ background: {c["border_strong"]}; border-radius: 3px;
                               min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {c["muted"]}; }}
QScrollBar::handle:vertical:pressed {{ background: {c["accent2"]}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 1px 2px; }}
QScrollBar::handle:horizontal {{ background: {c["border_strong"]}; border-radius: 3px;
                                 min-width: 28px; }}
QScrollBar::handle:horizontal:hover {{ background: {c["muted"]}; }}
QScrollBar::handle:horizontal:pressed {{ background: {c["accent2"]}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QLabel#mutedText {{ color: {c["muted"]}; }}
QLabel#accentText {{ color: {c["accent"]}; }}
QLabel#errorText {{ color: {c["danger"]}; }}
QLabel#warningText {{ color: {c["warning"]}; }}
QLabel#videoPlaceholder {{ color: #8fa1ba; font-size: 15px; }}
QFrame#videoFrame {{ background: {c["video_bg"]};
    border: 1px solid {c["border_strong"]}; border-radius: 12px; }}
QLabel#mediaCanvas {{ background: #05070b; color: #8fa1ba;
    border: 1px solid {c["border"]}; border-radius: 8px; }}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 15px; height: 15px; border: 1px solid {c["border_strong"]};
                        border-radius: 4px; background: {c["input"]}; }}
QCheckBox::indicator:hover {{ border-color: {c["accent"]}; }}
QCheckBox::indicator:checked {{ background: {c["accent"]};
                                border-color: {c["accent"]}; }}
QCheckBox::indicator:disabled {{ background: {c["surface2"]};
                                 border-color: {c["border"]}; }}
QRadioButton::indicator {{ width: 14px; height: 14px;
                           border: 1px solid {c["border_strong"]};
                           border-radius: 7px; background: {c["input"]}; }}
QRadioButton::indicator:checked {{ background: {c["accent"]};
                                   border: 3px solid {c["surface"]}; }}
"""
    if contrast:
        base += f"""
QPushButton, QToolButton {{ border: 2px solid #facc15; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox,
QPlainTextEdit, QTextEdit, QTableWidget, QListWidget {{
    border: 2px solid #facc15;
}}
QGroupBox {{ border: 2px solid #facc15; }}
QTabBar#workspaceTabBar::tab:selected, QTabBar::tab:selected {{
    border: 2px solid #facc15; border-bottom-color: #facc15;
}}
"""
    return base


def _palette(c: dict[str, str], dark: bool) -> QPalette:
    if not dark:
        palette = QApplication.style().standardPalette()
    else:
        palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(c["window"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(c["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(c["input"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(c["surface2"]))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(c["surface2"]))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(c["text"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(c["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(c["surface2"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(c["text"]))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(c["danger"]))
    palette.setColor(QPalette.ColorRole.Link, QColor(c["accent"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(c["accent2"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(c["on_accent"]))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(c["muted"]))
    palette.setColor(QPalette.ColorRole.Mid, QColor(c["border_strong"]))
    palette.setColor(QPalette.ColorRole.Dark, QColor(c["border"]))
    return palette


# Qt 标准图标在深色主题下可能仍是深色，统一按当前文字色重新着色。
_ICON_TARGETS: list[tuple[object, object, int]] = []


def _tinted_icon(
    icon: QIcon, color: QColor, size: int
) -> QIcon:
    pixmap = icon.pixmap(QSize(size, size))
    if pixmap.isNull():
        return icon
    tinted = QPixmap(pixmap.size())
    tinted.fill(Qt.GlobalColor.transparent)
    painter = QPainter(tinted)
    painter.drawPixmap(0, 0, pixmap)
    painter.setCompositionMode(
        QPainter.CompositionMode.CompositionMode_SourceIn
    )
    painter.fillRect(tinted.rect(), color)
    painter.end()
    return QIcon(tinted)


def apply_standard_icon(
    target, pixmap, size: int = 18
) -> None:
    """给 QAction/QToolButton 设置可随主题重新着色的标准图标。"""
    for index, (item, _pixmap, _size) in enumerate(_ICON_TARGETS):
        if item is target:
            _ICON_TARGETS[index] = (target, pixmap, size)
            break
    else:
        _ICON_TARGETS.append((target, pixmap, size))
    _refresh_icon(target, pixmap, size)


def _refresh_icon(target, pixmap, size: int) -> None:
    app = QApplication.instance()
    if app is None:
        return
    color = app.palette().color(QPalette.ColorRole.ButtonText)
    try:
        target.setIcon(_tinted_icon(app.style().standardIcon(pixmap), color, size))
    except RuntimeError:
        return


def refresh_theme_icons() -> None:
    app = QApplication.instance()
    if app is None:
        return
    color = app.palette().color(QPalette.ColorRole.ButtonText)
    alive: list[tuple[object, object, int]] = []
    for target, pixmap, size in _ICON_TARGETS:
        try:
            target.setIcon(
                _tinted_icon(app.style().standardIcon(pixmap), color, size)
            )
            alive.append((target, pixmap, size))
        except RuntimeError:
            continue
    _ICON_TARGETS[:] = alive


def apply_theme(app: QApplication, theme: str, *, scale: float = 1.0, contrast: bool = False) -> None:
    base_size = app.property("smartcut_base_font_size")
    if base_size is None:
        base_size = app.font().pointSizeF() or 9.0
        app.setProperty("smartcut_base_font_size", base_size)
    font = app.font()
    font.setPointSizeF(max(6.0, float(base_size) * max(0.75, min(2.0, scale))))
    app.setFont(font)

    dark = theme != "light"
    colors = _LIGHT if not dark else _DARK
    app.setPalette(_palette(colors, dark))
    app.setStyleSheet(_qss(colors, contrast))
    refresh_theme_icons()
