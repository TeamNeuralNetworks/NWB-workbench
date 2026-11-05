from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Optional, Callable, List

import pandas as pd

from GUI.qt_compat import QtCore, QtWidgets


def natural_sort_key(value):
    s = str(value).strip()
    parts = re.split(r"(\d+)", s)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


class FileTreePanel(QtWidgets.QWidget):
    """Qt replacement for the Tk FileTreePanel.

    Builds a tree from the inventory ``file_path`` column and emits the
    selected paths whenever the selection changes.
    """

    selection_changed = QtCore.Signal()
    add_requested = QtCore.Signal()
    delete_requested = QtCore.Signal()

    def __init__(self, df: pd.DataFrame, *, on_select: Optional[Callable] = None, parent=None):
        super().__init__(parent)
        self.df = df
        self.node_map: Dict[str, QtWidgets.QTreeWidgetItem] = {}
        self.common_root: Optional[Path] = None
        self._on_select = on_select

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        actions = QtWidgets.QHBoxLayout()
        self.add_btn = QtWidgets.QPushButton("Add NWB files")
        self.delete_btn = QtWidgets.QPushButton("Delete")
        self.add_btn.setToolTip("Add NWB files or scan a folder")
        self.delete_btn.setToolTip("Remove selected files from this project inventory")
        self.delete_btn.setEnabled(False)
        self.add_btn.clicked.connect(self.add_requested.emit)
        self.delete_btn.clicked.connect(self.delete_requested.emit)
        actions.addWidget(self.add_btn)
        actions.addWidget(self.delete_btn)
        actions.addStretch(1)
        layout.addLayout(actions)
        selection_actions = QtWidgets.QHBoxLayout()
        self.select_all_btn = QtWidgets.QPushButton("Select all")
        self.clear_selection_btn = QtWidgets.QPushButton("Clear selection")
        self.select_all_btn.setToolTip("Select all NWB files in the current filtered file list")
        self.select_all_btn.clicked.connect(self.select_all_files)
        self.clear_selection_btn.clicked.connect(self.clear_file_selection)
        selection_actions.addWidget(self.select_all_btn)
        selection_actions.addWidget(self.clear_selection_btn)
        layout.addLayout(selection_actions)

        self.tree = QtWidgets.QTreeWidget(self)
        self.tree.setHeaderHidden(True)
        try:
            selection_mode = QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection
        except AttributeError:
            selection_mode = QtWidgets.QAbstractItemView.ExtendedSelection
        self.tree.setSelectionMode(selection_mode)
        layout.addWidget(self.tree)

        self.tree.itemSelectionChanged.connect(self._selection_changed)

    def select_all_files(self):
        selection = QtCore.QItemSelection()
        for path, item in self.node_map.items():
            if path.lower().endswith(".nwb") and not item.isHidden():
                index = self.tree.indexFromItem(item)
                selection.select(index, index)
        previous = self.tree.blockSignals(True)
        updates = self.tree.updatesEnabled()
        self.tree.setUpdatesEnabled(False)
        try:
            flags = QtCore.QItemSelectionModel.SelectionFlag
            self.tree.selectionModel().select(selection, flags.ClearAndSelect | flags.Rows)
        finally:
            self.tree.setUpdatesEnabled(updates)
            self.tree.blockSignals(previous)
        self._selection_changed()

    def clear_file_selection(self):
        previous = self.tree.blockSignals(True)
        try:
            self.tree.clearSelection()
            self.tree.setCurrentItem(None)
        finally:
            self.tree.blockSignals(previous)
        self._selection_changed()

    def _selection_changed(self):
        self.delete_btn.setEnabled(bool(self.selected_inventory_paths()))
        self.selection_changed.emit()
        if self._on_select is not None:
            self._on_select()

    def selected_inventory_paths(self) -> List[str]:
        """Actual selected rows, including visible files below selected folders.

        Do not fall back to currentItem: it survives clearing the selection.
        Use path components so selecting /mouse1 never includes /mouse10.
        """
        if self.df is None or "file_path" not in self.df.columns:
            return []
        selected = {Path(self.item_to_path(item)) for item in self.tree.selectedItems()
                    if self.item_to_path(item)}
        if not selected:
            return []
        paths = []
        for value in self.df["file_path"].dropna().astype(str):
            if not value.lower().endswith(".nwb"):
                continue
            path = Path(value)
            if path in selected or any(parent in selected for parent in path.parents):
                paths.append(value)
        return list(dict.fromkeys(paths))

    def build_tree(self):
        self.delete_btn.setEnabled(False)
        self.tree.clear()
        self.node_map.clear()
        self.common_root = None

        if self.df is None or "file_path" not in self.df.columns:
            return

        df = self.df.copy()
        df["file_path"] = df["file_path"].astype(str)

        df = df[df["file_path"].str.lower().str.endswith(".nwb")]

        file_paths = df["file_path"].dropna().astype(str).tolist()
        if not file_paths:
            return

        try:
            common_root = os.path.commonpath(file_paths)
        except ValueError:
            # Windows files on different drives have no shared filesystem root.
            common_root = None
        self.common_root = Path(common_root) if common_root else None

        if self.common_root is not None:
            roots = [self.common_root]
        else:
            roots = sorted({Path(fp).anchor for fp in file_paths})
        for root in roots:
            root_path = str(root)
            root_item = QtWidgets.QTreeWidgetItem([root_path])
            root_item.setData(0, QtCore.Qt.UserRole, root_path)
            root_item.setExpanded(True)
            self.tree.addTopLevelItem(root_item)
            self.node_map[root_path] = root_item

        for fp in sorted(file_paths, key=natural_sort_key):
            try:
                self.insert_path(Path(fp))
            except Exception:
                continue

        # Tkinter kept the folder tree visually open. In Qt, rebuilding the
        # tree after each filter change collapses all newly-created items by
        # default, so force it open after every rebuild. ``singleShot`` lets Qt
        # finish laying out the new items first, which makes this reliable even
        # when build_tree() is called from a checkbox signal.
        QtCore.QTimer.singleShot(0, self.tree.expandAll)

    def insert_path(self, full_path: Path):
        root = self.common_root if self.common_root is not None else Path(full_path.anchor)
        relative_path = full_path.relative_to(root)
        parts = relative_path.parts
        parent = self.node_map[str(root)]
        current_path = root

        for i, part in enumerate(parts):
            current_path = current_path / part
            current_path_str = str(current_path)

            if current_path_str not in self.node_map:
                item = QtWidgets.QTreeWidgetItem([part])
                item.setData(0, QtCore.Qt.UserRole, current_path_str)
                # sorted insert among siblings
                insert_index = parent.childCount()
                part_key = natural_sort_key(part)
                for j in range(parent.childCount()):
                    if natural_sort_key(parent.child(j).text(0)) > part_key:
                        insert_index = j
                        break
                parent.insertChild(insert_index, item)
                self.node_map[current_path_str] = item

            parent = self.node_map[current_path_str]

    def item_to_path(self, item: QtWidgets.QTreeWidgetItem) -> str:
        stored_path = item.data(0, QtCore.Qt.UserRole)
        if stored_path:
            return str(stored_path)
        parts: List[str] = []
        node = item
        while node is not None:
            parts.insert(0, node.text(0))
            node = node.parent()
        return str(Path(*parts)) if parts else ""

    def selected_paths(self) -> List[str]:
        items = list(self.tree.selectedItems())
        if not items:
            item = self.tree.currentItem()
            items = [item] if item is not None else []
        return [self.item_to_path(item) for item in items if item is not None]

    def find_item_by_path(self, target_path: str):
        target_path = str(Path(target_path))
        return self.node_map.get(target_path)
