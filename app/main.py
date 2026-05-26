#!/usr/bin/env python3
"""RIFE Retime - VFX frame interpolation tool."""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont
from ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("RIFE Retime")
    app.setOrganizationName("vfx-local")

    qss_path = os.path.join(os.path.dirname(__file__), "assets", "style.qss")
    if os.path.exists(qss_path):
        with open(qss_path) as f:
            app.setStyleSheet(f.read())

    mono = QFont("Monospace", 10)
    mono.setStyleHint(QFont.StyleHint.Monospace)
    app.setFont(mono)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
