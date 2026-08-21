"""Central visual tokens and Qt stylesheet for the desktop interface."""

from __future__ import annotations


# region [01] Theme

COLORS = {
    "background": "#090D12",
    "surface": "#111821",
    "surface_alt": "#151E29",
    "border": "#243140",
    "text": "#F4F7FA",
    "muted": "#8A98A9",
    "accent": "#83D944",
    "accent_dark": "#182B16",
    "teal": "#2DD4BF",
    "amber": "#F5B942",
    "danger": "#FF626D",
}


STYLESHEET = """
* {
    font-family: "Segoe UI Variable", "Segoe UI";
    font-size: 10.5pt;
    color: #F4F7FA;
}
QMainWindow, QWidget#AppRoot { background: #090D12; }
QScrollArea#PageScroll, QWidget#PageCanvas { background: #090D12; }
QFrame#Sidebar {
    background: #0C1219;
    border-right: 1px solid #1E2936;
}
QLabel#BrandMark {
    color: #83D944;
    font-size: 22pt;
    font-weight: 800;
}
QLabel#BrandName { font-size: 14pt; font-weight: 750; }
QLabel#BrandCaption, QLabel[muted="true"] { color: #8A98A9; }
QPushButton#NavButton {
    background: transparent;
    border: 0;
    border-radius: 8px;
    color: #8A98A9;
    min-height: 42px;
    padding: 0 14px;
    text-align: left;
    font-weight: 600;
}
QPushButton#NavButton:hover { background: #121B25; color: #F4F7FA; }
QPushButton#NavButton:checked {
    background: #182B16;
    color: #A3EB6B;
    border-left: 3px solid #83D944;
}
QFrame#Header { background: #090D12; border-bottom: 1px solid #1E2936; }
QLabel#PageTitle { font-size: 20pt; font-weight: 750; }
QLabel#PageSubtitle { color: #8A98A9; }
QFrame#Panel, QFrame#MetricCard, QFrame#ProgressItem, QFrame#DependencyItem {
    background: #111821;
    border: 1px solid #243140;
    border-radius: 12px;
}
QFrame#ActivityBanner {
    background: #0D1C1C;
    border: 1px solid #22524D;
    border-radius: 10px;
}
QLabel#ActivityTitle { color: #52E0CC; font-size: 11pt; font-weight: 750; }
QLabel#ActivityDetail { color: #B7C5D1; font-size: 9.5pt; }
QFrame#MetricCard[accent="true"] { border: 1px solid #3C6328; }
QLabel#MetricTitle { color: #8A98A9; font-size: 9.5pt; font-weight: 650; }
QLabel#MetricValue { font-size: 22pt; font-weight: 760; }
QLabel#MetricDetail { color: #8A98A9; font-size: 9pt; }
QLabel#SectionTitle { font-size: 13pt; font-weight: 720; }
QLabel#SectionCaption { color: #8A98A9; font-size: 9.5pt; }
QLabel#StatusPill {
    border-radius: 10px;
    padding: 4px 10px;
    font-size: 9pt;
    font-weight: 700;
}
QLabel#StatusPill[state="idle"] { background: #202A36; color: #B9C4CF; }
QLabel#StatusPill[state="running"] { background: #173534; color: #52E0CC; }
QLabel#StatusPill[state="completed"] { background: #182F18; color: #9BE566; }
QLabel#StatusPill[state="warning"] { background: #382D15; color: #FFD36A; }
QLabel#StatusPill[state="failed"] { background: #3A1D24; color: #FF8992; }
QPushButton {
    background: #18222D;
    border: 1px solid #2A3948;
    border-radius: 8px;
    min-height: 38px;
    padding: 0 16px;
    font-weight: 650;
}
QPushButton:hover { background: #202D3A; border-color: #3A4B5C; }
QPushButton:pressed { background: #111922; }
QPushButton:disabled { color: #5E6A77; background: #111820; border-color: #1D2731; }
QPushButton#PrimaryButton {
    background: #83D944;
    color: #0B1308;
    border: 0;
    min-height: 44px;
    font-weight: 780;
}
QPushButton#PrimaryButton:hover { background: #96E65B; }
QPushButton#DangerButton { background: #3A1D24; color: #FF8992; border-color: #64303A; }
QPushButton#DangerButton:hover { background: #4A222B; }
QLineEdit, QComboBox, QSpinBox {
    background: #0C1219;
    border: 1px solid #2A3948;
    border-radius: 8px;
    min-height: 39px;
    padding: 0 11px;
    selection-background-color: #496F2E;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #83D944; }
QComboBox::drop-down { border: 0; width: 28px; }
QSpinBox::up-button, QSpinBox::down-button {
    background: #18222D;
    border-left: 1px solid #2A3948;
    width: 18px;
}
QComboBox QAbstractItemView {
    background: #111821;
    border: 1px solid #2A3948;
    selection-background-color: #263A22;
}
QCheckBox#RouteToggle {
    background: #0D141C;
    border: 1px solid #2A3948;
    border-radius: 9px;
    min-height: 42px;
    padding: 0 14px;
    font-weight: 650;
    spacing: 9px;
}
QCheckBox#RouteToggle:hover { border-color: #4A5D70; }
QCheckBox#RouteToggle:checked { background: #182B16; border-color: #669E3D; color: #A8EA76; }
QCheckBox#RouteToggle::indicator { width: 15px; height: 15px; }
QRadioButton#ModeButton {
    background: #0C1219;
    border: 1px solid #2A3948;
    border-radius: 8px;
    min-height: 40px;
    padding: 0 16px;
    spacing: 8px;
    font-weight: 650;
}
QRadioButton#ModeButton:checked { background: #1B2D18; border-color: #669E3D; color: #A8EA76; }
QRadioButton#ModeButton[danger="true"]:checked { background: #3A1D24; border-color: #7B3743; color: #FF9AA2; }
QProgressBar {
    background: #0B1118;
    border: 0;
    border-radius: 4px;
    min-height: 8px;
    max-height: 8px;
    color: transparent;
}
QProgressBar::chunk { background: #83D944; border-radius: 4px; }
QPlainTextEdit {
    background: #0A1016;
    border: 1px solid #243140;
    border-radius: 8px;
    padding: 9px;
    font-family: "Cascadia Mono", "Consolas";
    font-size: 9.5pt;
}
QTableWidget {
    background: #0D141C;
    alternate-background-color: #101924;
    border: 1px solid #243140;
    border-radius: 10px;
    gridline-color: #1E2A36;
    selection-background-color: #263A22;
}
QHeaderView::section {
    background: #151E29;
    color: #9CA9B7;
    border: 0;
    border-bottom: 1px solid #2A3948;
    padding: 9px;
    font-weight: 700;
}
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #344251; border-radius: 4px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QToolTip { background: #1B2632; color: #F4F7FA; border: 1px solid #3A4B5C; padding: 5px; }
"""


# endregion [01]
