from __future__ import annotations

from pathlib import Path

import pandas as pd

from GUI.qt_compat import (
    QtCore,
    QtGui,
    QtWidgets,
    qt_checked,
    qt_unchecked,
    qt_user_role,
)
from Tools.General.preprocessed_data_inventory import (
    build_preprocessed_deletion_plan,
    execute_preprocessed_deletion,
    folder_size_bytes,
    format_file_size,
    inventory_rows_with_storage,
    preprocessed_cache_root,
    preprocessed_inventory_path,
)


_ALIGNMENT_COLUMNS = {
    "start_event_marker": "Start marker",
    "start_edge": "Start edge",
    "start_offset_s": "Start offset (s)",
    "end_event_marker": "End marker",
    "end_edge": "End edge",
    "end_offset_s": "End offset (s)",
}


def _alignment_display_rows(rows):
    """Project cache metadata into readable boundaries, without changing disk data."""
    display = rows.copy()

    def present(value):
        return value is not None and not pd.isna(value) and str(value).strip() != ""

    def value_or(row, key, fallback):
        value = row.get(key)
        return value if present(value) else fallback

    boundaries = []
    for _, row in rows.iterrows():
        marker = value_or(row, "start_event_marker", row.get("aligned_to_event"))
        if not present(marker) or str(marker).strip().lower() in ("false", "none", "0", "0.0"):
            boundaries.append({key: "" for key in _ALIGNMENT_COLUMNS})
            continue
        edge = value_or(row, "start_edge", row.get("edge", ""))
        start_offset = row.get("start_offset_s")
        if not present(start_offset):
            before = row.get("pad_before_s")
            try:
                start_offset = -float(before) if present(before) else ""
                if start_offset == 0:
                    start_offset = 0.0
            except (TypeError, ValueError):
                start_offset = ""
        boundaries.append({
            "start_event_marker": marker,
            "start_edge": edge,
            "start_offset_s": start_offset,
            # A legacy cache used the same reference for both boundaries.
            "end_event_marker": value_or(row, "end_event_marker", marker),
            "end_edge": value_or(row, "end_edge", edge),
            "end_offset_s": value_or(row, "end_offset_s", row.get("pad_after_s", "")),
        })
    for key in _ALIGNMENT_COLUMNS:
        display[key] = [entry[key] for entry in boundaries]
    old_fields = {"aligned_to_event", "edge", "pad_before_s", "pad_after_s", "alignment_signature"}
    columns = []
    inserted = False
    for column in rows.columns:
        if column in old_fields or column in _ALIGNMENT_COLUMNS:
            if not inserted:
                columns.extend(_ALIGNMENT_COLUMNS)
                inserted = True
        else:
            columns.append(column)
    if not inserted:
        columns.extend(_ALIGNMENT_COLUMNS)
    if "computed_at" not in display.columns:
        display["computed_at"] = ""
    if "computed_at" in columns:
        columns.remove("computed_at")
    position = columns.index("data_type") + 1 if "data_type" in columns else 0
    columns.insert(position, "computed_at")
    return display[columns]



class _FolderSizeWorker(QtCore.QObject):
    size_ready = QtCore.Signal(int, object)
    finished = QtCore.Signal()

    def __init__(self, entries, parent=None):
        super().__init__(parent)
        self.entries = list(entries or [])

    def run(self):
        try:
            thread = QtCore.QThread.currentThread()
            for inventory_row, path in self.entries:
                if thread.isInterruptionRequested():
                    break
                size_bytes = folder_size_bytes(
                    Path(path),
                    stop_requested=thread.isInterruptionRequested,
                )
                if thread.isInterruptionRequested():
                    break
                self.size_ready.emit(int(inventory_row), int(size_bytes))
        finally:
            self.finished.emit()


class _SizeTableItem(QtWidgets.QTableWidgetItem):
    def __lt__(self, other):
        left = self.data(qt_user_role())
        right = other.data(qt_user_role()) if other is not None else None
        if left is not None and right is not None:
            try:
                return int(left) < int(right)
            except (TypeError, ValueError):
                pass
        return super().__lt__(other)


class PreprocessedDataManager(QtWidgets.QWidget):
    """Project-level browser and safe deletion manager for analysis caches."""

    inventory_changed = QtCore.Signal()

    def __init__(self, *, project_root, is_busy=None, parent=None):
        super().__init__(parent)
        self.project_root = Path(project_root).expanduser().resolve()
        self.is_busy = is_busy if callable(is_busy) else (lambda: False)
        self._inventory_rows = pd.DataFrame()
        self._size_items_by_inventory_row = {}
        self._size_by_inventory_row = {}
        self._size_thread = None
        self._size_worker = None
        self._refresh_pending = False

        self._build_ui()
        application = QtWidgets.QApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self.stop_size_scan)

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)

        header = QtWidgets.QHBoxLayout()
        heading = QtWidgets.QVBoxLayout()
        title = QtWidgets.QLabel("Preprocessed data")
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        heading.addWidget(title)
        self.location_lbl = QtWidgets.QLabel(
            f"Inventory: {preprocessed_inventory_path(self.project_root)}"
        )
        self.location_lbl.setWordWrap(True)
        self.location_lbl.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
            if hasattr(QtCore.Qt, "TextInteractionFlag")
            else QtCore.Qt.TextSelectableByMouse
        )
        self.location_lbl.setStyleSheet("color: #555;")
        heading.addWidget(self.location_lbl)
        header.addLayout(heading, stretch=1)

        self.refresh_btn = QtWidgets.QPushButton("Refresh", self)
        self.refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self.refresh_btn)
        self.open_folder_btn = QtWidgets.QPushButton("Open folder", self)
        self.open_folder_btn.clicked.connect(self._open_selected_folder)
        header.addWidget(self.open_folder_btn)
        layout.addLayout(header)

        self.summary_lbl = QtWidgets.QLabel("", self)
        self.summary_lbl.setWordWrap(True)
        layout.addWidget(self.summary_lbl)

        self.table = QtWidgets.QTableWidget(self)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().setVisible(False)
        no_edit = (
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
            if hasattr(QtWidgets.QAbstractItemView, "EditTrigger")
            else QtWidgets.QAbstractItemView.NoEditTriggers
        )
        select_rows = (
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
            if hasattr(QtWidgets.QAbstractItemView, "SelectionBehavior")
            else QtWidgets.QAbstractItemView.SelectRows
        )
        single_selection = (
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
            if hasattr(QtWidgets.QAbstractItemView, "SelectionMode")
            else QtWidgets.QAbstractItemView.SingleSelection
        )
        self.table.setEditTriggers(no_edit)
        self.table.setSelectionBehavior(select_rows)
        self.table.setSelectionMode(single_selection)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._update_controls)
        self.table.cellDoubleClicked.connect(lambda _row, _column: self._open_selected_folder())
        layout.addWidget(self.table, stretch=1)

        controls = QtWidgets.QHBoxLayout()
        self.select_all_btn = QtWidgets.QPushButton("Select all", self)
        self.select_all_btn.clicked.connect(lambda: self._set_all_checkboxes("all"))
        controls.addWidget(self.select_all_btn)
        self.unselect_all_btn = QtWidgets.QPushButton("Unselect all", self)
        self.unselect_all_btn.clicked.connect(lambda: self._set_all_checkboxes("none"))
        controls.addWidget(self.unselect_all_btn)
        self.reverse_selection_btn = QtWidgets.QPushButton("Reverse selection", self)
        self.reverse_selection_btn.clicked.connect(lambda: self._set_all_checkboxes("reverse"))
        controls.addWidget(self.reverse_selection_btn)
        controls.addStretch(1)
        self.delete_btn = QtWidgets.QPushButton("Delete selected", self)
        self.delete_btn.clicked.connect(self._delete_selected)
        controls.addWidget(self.delete_btn)
        layout.addLayout(controls)

        self._update_controls()

    @staticmethod
    def _display_value(value) -> str:
        if value is None:
            return ""
        try:
            if pd.isna(value):
                return ""
        except (TypeError, ValueError):
            pass
        if isinstance(value, bool):
            return "Yes" if value else "No"
        return str(value)

    @staticmethod
    def _item_is_checkable(item) -> bool:
        if item is None:
            return False
        flag = (
            QtCore.Qt.ItemFlag.ItemIsUserCheckable
            if hasattr(QtCore.Qt, "ItemFlag")
            else QtCore.Qt.ItemIsUserCheckable
        )
        return bool(item.flags() & flag)

    def refresh(self):
        if self._size_thread is not None and self._size_thread.isRunning():
            self._refresh_pending = True
            self._size_thread.requestInterruption()
            return

        self._refresh_pending = False
        try:
            rows = inventory_rows_with_storage(
                self.project_root,
                calculate_sizes=False,
            )
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                self,
                "Preprocessed data",
                f"Could not read the preprocessing inventory:\n{exc}",
            )
            return

        self._inventory_rows = rows
        self._size_items_by_inventory_row = {}
        self._size_by_inventory_row = {}
        display_rows = _alignment_display_rows(rows)
        inventory_columns = [
            column
            for column in display_rows.columns
            if not str(column).startswith("_")
        ]
        headers = ["Delete"] + ["Computed at" if c == "computed_at" else _ALIGNMENT_COLUMNS.get(c, c) for c in inventory_columns] + ["Size", "Status"]

        blocked = self.table.blockSignals(True)
        self.table.setSortingEnabled(False)
        try:
            self.table.clear()
            self.table.setColumnCount(len(headers))
            self.table.setHorizontalHeaderLabels(headers)
            self.table.setRowCount(len(rows))
            for table_row, (_, row) in enumerate(display_rows.iterrows()):
                inventory_row = int(row.get("_inventory_row", table_row))
                status = str(row.get("_status", ""))

                checkbox = QtWidgets.QTableWidgetItem("")
                checkbox.setData(qt_user_role(), inventory_row)
                selectable = (
                    QtCore.Qt.ItemFlag.ItemIsSelectable
                    if hasattr(QtCore.Qt, "ItemFlag")
                    else QtCore.Qt.ItemIsSelectable
                )
                enabled = (
                    QtCore.Qt.ItemFlag.ItemIsEnabled
                    if hasattr(QtCore.Qt, "ItemFlag")
                    else QtCore.Qt.ItemIsEnabled
                )
                checkable = (
                    QtCore.Qt.ItemFlag.ItemIsUserCheckable
                    if hasattr(QtCore.Qt, "ItemFlag")
                    else QtCore.Qt.ItemIsUserCheckable
                )
                flags = selectable | enabled
                if status in ("Available", "Missing on disk"):
                    flags |= checkable
                    checkbox.setCheckState(qt_unchecked())
                else:
                    checkbox.setToolTip(
                        "This entry cannot be deleted automatically because its path is not safe."
                    )
                checkbox.setFlags(flags)
                self.table.setItem(table_row, 0, checkbox)

                for column_index, column_name in enumerate(inventory_columns, start=1):
                    item = QtWidgets.QTableWidgetItem(
                        self._display_value(row.get(column_name, ""))
                    )
                    item.setToolTip(item.text())
                    self.table.setItem(table_row, column_index, item)

                size_item = _SizeTableItem(
                    "Calculating..." if status == "Available" else ""
                )
                size_item.setTextAlignment(
                    int(
                        QtCore.Qt.AlignmentFlag.AlignRight
                        | QtCore.Qt.AlignmentFlag.AlignVCenter
                    )
                    if hasattr(QtCore.Qt, "AlignmentFlag")
                    else int(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                )
                self.table.setItem(table_row, len(headers) - 2, size_item)
                self._size_items_by_inventory_row[inventory_row] = size_item

                status_item = QtWidgets.QTableWidgetItem(status)
                if status != "Available":
                    status_item.setForeground(QtGui.QBrush(QtGui.QColor("#b00020")))
                self.table.setItem(table_row, len(headers) - 1, status_item)
        finally:
            self.table.blockSignals(blocked)

        self.table.resizeColumnsToContents()
        if "save_path" in inventory_columns:
            save_path_column = 1 + inventory_columns.index("save_path")
            self.table.setColumnWidth(
                save_path_column,
                min(420, max(180, self.table.columnWidth(save_path_column))),
            )
        self.table.resizeRowsToContents()
        self.table.setSortingEnabled(True)
        self._update_summary(scanning=bool(len(rows)))
        self._update_controls()
        self._start_size_scan()

    def _start_size_scan(self):
        entries = []
        for _, row in self._inventory_rows.iterrows():
            if str(row.get("_status", "")) != "Available":
                continue
            path = str(row.get("_resolved_path", "") or "")
            if path:
                entries.append((int(row.get("_inventory_row")), path))
        if not entries:
            self._finish_size_scan()
            return

        self.refresh_btn.setEnabled(False)
        thread = QtCore.QThread(self)
        worker = _FolderSizeWorker(entries)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.size_ready.connect(self._on_size_ready)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._finish_size_scan)
        thread.finished.connect(thread.deleteLater)
        self._size_thread = thread
        self._size_worker = worker
        thread.start()

    def _on_size_ready(self, inventory_row: int, size_bytes: int):
        self._size_by_inventory_row[int(inventory_row)] = int(size_bytes)
        item = self._size_items_by_inventory_row.get(int(inventory_row))
        if item is not None:
            item.setText(format_file_size(size_bytes))
            item.setData(qt_user_role(), int(size_bytes))
        self._update_summary(scanning=True)

    def _finish_size_scan(self):
        self._size_thread = None
        self._size_worker = None
        self.refresh_btn.setEnabled(True)
        self._update_summary(scanning=False)
        self._update_controls()
        if self._refresh_pending:
            self._refresh_pending = False
            QtCore.QTimer.singleShot(0, self.refresh)

    def _update_summary(self, *, scanning: bool):
        count = len(self._inventory_rows)
        available = 0
        missing = 0
        if not self._inventory_rows.empty:
            statuses = self._inventory_rows.get("_status", pd.Series(dtype=str)).astype(str)
            available = int((statuses == "Available").sum())
            missing = int((statuses == "Missing on disk").sum())
        total_size = sum(self._size_by_inventory_row.values())
        text = f"{count} cached item(s) | {available} folder(s) on disk"
        if scanning:
            text += f" | Size calculated so far: {format_file_size(total_size)}"
        else:
            text += f" | Total size: {format_file_size(total_size)}"
        if missing:
            text += f" | {missing} missing folder(s)"
        self.summary_lbl.setText(text)

    def _checked_inventory_rows(self) -> list[int]:
        selected = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if (
                self._item_is_checkable(item)
                and item.checkState() == qt_checked()
            ):
                selected.append(int(item.data(qt_user_role())))
        return selected

    def _set_all_checkboxes(self, mode: str):
        blocked = self.table.blockSignals(True)
        try:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if not self._item_is_checkable(item):
                    continue
                checked = item.checkState() == qt_checked()
                if mode == "all":
                    new_state = qt_checked()
                elif mode == "none":
                    new_state = qt_unchecked()
                elif mode == "reverse":
                    new_state = qt_unchecked() if checked else qt_checked()
                else:
                    return
                item.setCheckState(new_state)
        finally:
            self.table.blockSignals(blocked)
        self._update_controls()

    def _on_item_changed(self, item):
        if item is not None and item.column() == 0:
            self._update_controls()

    def _update_controls(self):
        count = self.table.rowCount()
        checked_count = len(self._checked_inventory_rows())
        scanning = self._size_thread is not None and self._size_thread.isRunning()
        busy = bool(self.is_busy())
        self.select_all_btn.setEnabled(count > 0 and not busy)
        self.unselect_all_btn.setEnabled(count > 0 and not busy)
        self.reverse_selection_btn.setEnabled(count > 0 and not busy)
        self.delete_btn.setEnabled(checked_count > 0 and not busy and not scanning)

        selected_rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        self.open_folder_btn.setEnabled(len(selected_rows) == 1)

    def _row_inventory_index(self, table_row: int) -> int | None:
        item = self.table.item(int(table_row), 0)
        if item is None:
            return None
        value = item.data(qt_user_role())
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _resolved_path_for_inventory_row(self, inventory_row: int) -> Path | None:
        matches = self._inventory_rows[
            self._inventory_rows["_inventory_row"] == int(inventory_row)
        ]
        if len(matches) != 1:
            return None
        text = str(matches.iloc[0].get("_resolved_path", "") or "")
        return Path(text) if text else None

    def _open_selected_folder(self):
        selected_rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if len(selected_rows) != 1:
            return
        inventory_row = self._row_inventory_index(selected_rows[0].row())
        path = (
            self._resolved_path_for_inventory_row(inventory_row)
            if inventory_row is not None
            else None
        )
        if path is None or not path.exists():
            QtWidgets.QMessageBox.warning(
                self,
                "Preprocessed data",
                "The selected cache folder does not exist.",
            )
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))

    def _delete_selected(self):
        if self.is_busy():
            QtWidgets.QMessageBox.warning(
                self,
                "Preprocessed data",
                "Wait for the current analysis or analysis pipeline to finish before deleting cached data.",
            )
            return
        selected = self._checked_inventory_rows()
        if not selected:
            return
        try:
            plan = build_preprocessed_deletion_plan(self.project_root, selected)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Preprocessed data", str(exc))
            self.refresh()
            return

        message = (
            f"Delete {len(plan.removed_row_indices)} preprocessed-data entr"
            f"{'y' if len(plan.removed_row_indices) == 1 else 'ies'} and "
            f"{len(plan.target_paths) - len(plan.missing_paths)} associated folder(s)?\n\n"
            f"Disk space to free: {format_file_size(plan.size_bytes)}"
        )
        if plan.missing_paths:
            message += (
                f"\nMissing folders: {len(plan.missing_paths)} "
                "(their stale inventory entries will be removed)"
            )
        message += "\n\nThis deletion cannot be undone."
        answer = QtWidgets.QMessageBox.question(
            self,
            "Delete preprocessed data?",
            message,
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return

        wait_cursor = (
            QtCore.Qt.CursorShape.WaitCursor
            if hasattr(QtCore.Qt, "CursorShape")
            else QtCore.Qt.WaitCursor
        )
        QtWidgets.QApplication.setOverrideCursor(wait_cursor)
        try:
            result = execute_preprocessed_deletion(plan)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                self,
                "Preprocessed data",
                f"Could not delete the selected preprocessed data:\n{exc}",
            )
            self.refresh()
            return
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

        self.inventory_changed.emit()
        self.refresh()
        summary = (
            f"Deleted {result['removed_rows']} inventory entr"
            f"{'y' if result['removed_rows'] == 1 else 'ies'} and "
            f"{result['removed_folders']} folder(s).\n"
            f"Freed approximately {format_file_size(result['size_bytes'])}."
        )
        if result.get("warning"):
            QtWidgets.QMessageBox.warning(
                self,
                "Preprocessed data",
                summary + "\n\n" + str(result["warning"]),
            )
        else:
            QtWidgets.QMessageBox.information(self, "Preprocessed data", summary)

    def cache_folder(self) -> Path:
        return preprocessed_cache_root(self.project_root)

    def refresh_busy_state(self):
        self._update_controls()

    def stop_size_scan(self):
        thread = self._size_thread
        if thread is None or not thread.isRunning():
            return
        self._refresh_pending = False
        thread.requestInterruption()
        thread.quit()
        thread.wait(3000)
