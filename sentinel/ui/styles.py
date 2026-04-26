"""
UI styles for the Sentinel Home application.
"""

# Dark theme with cyan accents
DARK_CYAN_THEME = """
QMainWindow, QWidget {
    background-color: #2E2E2E;
    color: #00FFFF;
}
QGroupBox {
    border: 1px solid #00FFFF;
    border-radius: 5px;
    margin-top: 10px;
    color: #00FFFF;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 3px;
    color: #00FFFF;
}
QPushButton, QRadioButton {
    background-color: #00FFFF;
    color: #2E2E2E;
    border: none;
    padding: 5px;
    border-radius: 3px;
}
QPushButton[inactive="true"] {
    background-color: #555555;
    color: #999999;
    border: 1px solid #444444;
}
QPushButton:hover, QRadioButton:hover {
    background-color: #00CCCC;
}
QComboBox, QSlider, QLabel {
    color: #00FFFF;
}
QComboBox {
    background-color: #3E3E3E;
    border: 1px solid #00FFFF;
    padding: 2px;
}
QComboBox::drop-down {
    border: none;
}
QSlider::groove:horizontal {
    background: #3E3E3E;
    height: 8px;
    border-radius: 4px;
}
QSlider::handle:horizontal {
    background: #00FFFF;
    width: 16px;
    height: 16px;
    border-radius: 8px;
    margin: -4px 0;
}
"""

# Light theme (for future use)
LIGHT_THEME = """
QMainWindow, QWidget {
    background-color: #F0F0F0;
    color: #333333;
}
QGroupBox {
    border: 1px solid #666666;
    border-radius: 5px;
    margin-top: 10px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 3px;
}
QPushButton, QRadioButton {
    background-color: #4DA6FF;
    color: white;
    border: none;
    padding: 5px;
    border-radius: 3px;
}
QPushButton:hover, QRadioButton:hover {
    background-color: #3385CC;
}
"""
