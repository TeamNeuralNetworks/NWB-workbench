from __future__ import annotations

from typing import Optional, Callable

from GUI.qt_compat import QtCore, QtWidgets


class ContentPanel(QtWidgets.QWidget):
    """Qt replacement for the Tk ContentPanel."""

    item_clicked = QtCore.Signal(object)
    running_view_requested = QtCore.Signal()

    def __init__(self, *, on_item_click: Optional[Callable] = None, parent=None):
        super().__init__(parent)
        self._on_item_click = on_item_click

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.jobs_frame = QtWidgets.QFrame(self)
        self.jobs_frame.setVisible(False)
        self.jobs_frame.setStyleSheet(
            "QFrame { background: #f6faf7; color: #2e7d32; "
            "border: 1px solid #cfe8d2; border-radius: 4px; } "
            "QLabel { color: #2e7d32; border: none; }"
        )
        jobs_layout = QtWidgets.QHBoxLayout(self.jobs_frame)
        jobs_layout.setContentsMargins(5, 3, 5, 3)
        jobs_layout.setSpacing(5)
        self.jobs_label = QtWidgets.QLabel("", self.jobs_frame)
        self.jobs_label.setWordWrap(True)
        jobs_layout.addWidget(self.jobs_label, stretch=1)
        self.jobs_button = QtWidgets.QPushButton("Show progress", self.jobs_frame)
        self.jobs_button.setToolTip("Show the active NWB object operation and its progress.")
        self.jobs_button.clicked.connect(
            lambda _checked=False: self.running_view_requested.emit()
        )
        jobs_layout.addWidget(self.jobs_button)
        layout.addWidget(self.jobs_frame)

        self.tree = QtWidgets.QTreeWidget(self)
        self.tree.setHeaderHidden(True)
        layout.addWidget(self.tree)

        self.tree.itemClicked.connect(self._item_clicked)

    def _item_clicked(self, item, _column):
        self.item_clicked.emit(item)
        if self._on_item_click is not None:
            self._on_item_click(item)

    def clear(self):
        self.tree.clear()

    def set_running_state(self, running: bool, message: str = ""):
        """Keep the active NWB edit visible while the user browses elsewhere."""
        if running:
            text = str(message or "NWB object edit is running").strip()
            self.jobs_label.setText(text)
            self.jobs_frame.setVisible(True)
        else:
            self.jobs_frame.setVisible(False)
            self.jobs_button.clearFocus()

    def insert(self, parent, text: str, *, open_: bool = False):
        item = QtWidgets.QTreeWidgetItem([str(text)])
        if parent is None:
            self.tree.addTopLevelItem(item)
        else:
            parent.addChild(item)
        item.setExpanded(bool(open_))
        return item
