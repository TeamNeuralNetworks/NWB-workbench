from __future__ import annotations

from pathlib import Path
import shutil

from GUI.qt_compat import QtCore, QtGui, QtWidgets
from GUI.panels.new_spike_sorting_dialog import NewSpikeSortingDialog
from GUI.panels.spike_sorting_right_views import (
    SpikeSortingSummaryView,
    SpikeSortingPreprocessingView,
    SpikeSortingSortingView,
    SpikeSortingCurationView,
)


class _SpikeSortingJobManager(QtCore.QObject):
    """Central lightweight job monitor for spike-sorting views.

    The long-running workers still live inside their view widgets for now, but
    this manager is the single place that tracks which cached views are busy.
    It lets the left tree show background jobs and makes the next refactor
    toward fully detached worker ownership much smaller.
    """

    jobsChanged = QtCore.Signal()

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self._states = {}

    def attach_view(self, view_key: str, view):
        view_key = str(view_key or "")
        if not view_key or view is None:
            return
        connected = getattr(view, "_spike_sorting_panel_job_keys", None)
        if connected is None:
            connected = set()
            try:
                setattr(view, "_spike_sorting_panel_job_keys", connected)
            except Exception:
                connected = set()
        if view_key in connected:
            return
        signal = getattr(view, "busyStateChanged", None)
        if signal is not None:
            try:
                signal.connect(lambda *_args, _self=self: _self.poll())
                connected.add(view_key)
            except Exception:
                pass

    def poll(self, emit: bool = True):
        panel = self.panel
        states = {}
        cache = getattr(panel, "_right_view_cache", {}) or {}
        for view_key, view in list(cache.items()):
            if view is None:
                continue
            if not panel._is_right_view_busy(view):
                continue
            view_key = str(view_key)
            sorting_key = panel._sorting_key_from_view_key(view_key)
            section = view_key.rsplit("::", 1)[-1] if "::" in view_key else "Summary"
            sorting_name = ""
            try:
                sorting_name = str((panel.sorting_data_by_key.get(sorting_key) or {}).get("name", "") or "")
            except Exception:
                sorting_name = ""
            text = f"{section} is running"
            if sorting_name:
                text = f"{section} is running in {sorting_name}"
            states[view_key] = {
                "sorting_key": sorting_key,
                "sorting_name": sorting_name,
                "section": section,
                "text": text,
            }
        if states != self._states:
            self._states = states
            if emit:
                self.jobsChanged.emit()
        return states

    def busy_view_keys(self):
        self.poll(emit=False)
        return set(self._states.keys())

    def busy_sorting_keys(self):
        self.poll(emit=False)
        return {str(v.get("sorting_key", "")) for v in self._states.values() if v.get("sorting_key")}

    def tooltip_for_key(self, key: str) -> str:
        key = str(key or "")
        self.poll(emit=False)
        if key in self._states:
            return self._states[key].get("text", "Running in the background")
        lines = []
        for state in self._states.values():
            if state.get("sorting_key") == key:
                lines.append(state.get("text", "Running in the background"))
        return "\n".join(lines)


class SpikeSortingPanel(QtWidgets.QWidget):
    """Qt migration of the spike sorting left panel.

    This step migrates the sorting list, New dialog, Delete, selection, and
    right-side section placeholders while keeping the same recipe mechanism.
    """

    SORTING_SECTIONS = ("Preprocessing", "Sorting", "Curation")

    def __init__(self, parent=None, *, right_panel=None, GUI_instance=None):
        super().__init__(parent)
        self.right_panel = right_panel
        self.GUI_instance = GUI_instance
        self.selected_file_list = []
        self.sorting_root = self._get_sorting_root()
        self.selected_sorting_key = None
        self.selected_view_key = None
        self.sorting_data_by_key = {}
        self.section_data_by_key = {}
        self.new_sorting_dialog = None
        self._current_right_view = None
        self._current_right_view_key = None
        # Stage 1 background browsing: keep created right-panel views alive.
        # This lets long-running QThreads continue safely while the user opens
        # another spike-sorting section. Hidden views are not destroyed until
        # their sorting is deleted or the app exits.
        self._right_view_cache = {}
        # Subtle running-state refresh.  Do not blink the whole tree; only the
        # exact running section gets a light highlight.
        self._blink_running_sections = False
        self.job_manager = _SpikeSortingJobManager(self)
        self.job_manager.jobsChanged.connect(self._apply_selection_highlight)
        self._job_indicator_timer = QtCore.QTimer(self)
        self._job_indicator_timer.setInterval(900)
        self._job_indicator_timer.timeout.connect(self._on_job_indicator_timer)
        self._job_indicator_timer.start()
        self._build_ui()
        self.refresh_sorting_list()

    def _get_sorting_root(self) -> Path:
        gui = self.GUI_instance
        if gui is not None and hasattr(gui, "project_root"):
            return Path(gui.project_root) / "Data" / "Spike_sorting"
        if gui is not None and hasattr(gui, "NWB_inventory_path"):
            return Path(gui.NWB_inventory_path).parent / "Spike_sorting"
        return Path("Data") / "Spike_sorting"

    def _build_ui(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        top = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Spike sorting")
        f = title.font(); f.setPointSize(max(f.pointSize(), 12)); f.setBold(True); title.setFont(f)
        top.addWidget(title)
        top.addStretch(1)
        self.new_btn = QtWidgets.QPushButton("New")
        self.delete_btn = QtWidgets.QPushButton("Delete")
        self.delete_btn.setEnabled(False)
        self.new_btn.clicked.connect(self.open_new_sorting_dialog)
        self.delete_btn.clicked.connect(self.delete_spike_sorting)
        top.addWidget(self.new_btn)
        top.addWidget(self.delete_btn)
        outer.addLayout(top)

        self.jobs_label = QtWidgets.QLabel("", self)
        self.jobs_label.setVisible(False)
        self.jobs_label.setWordWrap(True)
        self.jobs_label.setStyleSheet(
            "QLabel { background: #f6faf7; color: #2e7d32; "
            "border: 1px solid #cfe8d2; border-radius: 4px; padding: 3px 5px; }"
        )
        outer.addWidget(self.jobs_label)

        self.tree = QtWidgets.QTreeWidget(self)
        self.tree.setHeaderHidden(True)
        self.tree.itemClicked.connect(self._on_item_clicked)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        outer.addWidget(self.tree, stretch=1)

    def _on_job_indicator_timer(self):
        # Poll background jobs periodically, but keep the visual indication calm.
        self._blink_running_sections = False
        try:
            self.job_manager.poll()
        except Exception:
            pass
        self._apply_selection_highlight()

    def _load_saved_sortings(self):
        root = self.sorting_root
        if not root.exists() or not root.is_dir():
            return []
        return [p for p in sorted(root.iterdir(), key=lambda p: p.name.lower()) if p.is_dir() and not p.name.startswith(".")]

    def _sorting_key(self, name: str, sorting_path: Path | None, is_pending: bool = False) -> str:
        if sorting_path is not None:
            return f"saved::{sorting_path}"
        if is_pending:
            return f"pending::{name}"
        return f"sorting::{name}"

    def refresh_sorting_list(self):
        self.tree.clear()
        self.sorting_data_by_key.clear()
        self.section_data_by_key.clear()
        saved_sortings = self._load_saved_sortings()
        valid_keys = set()
        valid_view_keys = set()

        if not saved_sortings:
            item = QtWidgets.QTreeWidgetItem(["No spike sorting yet."])
            item.setDisabled(True)
            self.tree.addTopLevelItem(item)
            self.delete_btn.setEnabled(False)
            self._drop_stale_cached_views(set())
            return

        for sorting_path in saved_sortings:
            sorting_key = self._sorting_key(sorting_path.name, sorting_path, False)
            valid_keys.add(sorting_key)
            valid_view_keys.add(sorting_key)
            info = {
                "name": sorting_path.name,
                "path": sorting_path,
                "config": None,
                "is_pending": False,
                "GUI_instance": self.GUI_instance,
            }
            self.sorting_data_by_key[sorting_key] = info
            top = QtWidgets.QTreeWidgetItem([sorting_path.name])
            top.setData(0, QtCore.Qt.ItemDataRole.UserRole if hasattr(QtCore.Qt, "ItemDataRole") else QtCore.Qt.UserRole, sorting_key)
            font = top.font(0); font.setBold(True); top.setFont(0, font)
            self.tree.addTopLevelItem(top)
            for section in self.SORTING_SECTIONS:
                section_key = f"{sorting_key}::{section}"
                valid_view_keys.add(section_key)
                self.section_data_by_key[section_key] = {
                    "name": sorting_path.name,
                    "sorting_name": sorting_path.name,
                    "section": section,
                    "path": sorting_path,
                    "config": None,
                    "is_pending": False,
                    "GUI_instance": self.GUI_instance,
                }
                child = QtWidgets.QTreeWidgetItem([section])
                child.setData(0, QtCore.Qt.ItemDataRole.UserRole if hasattr(QtCore.Qt, "ItemDataRole") else QtCore.Qt.UserRole, section_key)
                top.addChild(child)
            top.setExpanded(sorting_key == self.selected_sorting_key or self.selected_sorting_key is None)

        if self.selected_sorting_key not in valid_keys:
            self.selected_sorting_key = None
            self.selected_view_key = None
            self.delete_btn.setEnabled(False)

        self._drop_stale_cached_views(valid_view_keys)
        self._apply_selection_highlight()

    def _user_role_value(self, item):
        role = QtCore.Qt.ItemDataRole.UserRole if hasattr(QtCore.Qt, "ItemDataRole") else QtCore.Qt.UserRole
        return item.data(0, role)

    def _on_item_clicked(self, item, _column=0):
        key = self._user_role_value(item)
        if not key:
            return
        if key in self.sorting_data_by_key:
            self._select_sorting(key)
        elif key in self.section_data_by_key:
            self._on_section_clicked(key)

    def _on_item_double_clicked(self, item, _column=0):
        if item.childCount() > 0:
            item.setExpanded(not item.isExpanded())

    def _select_sorting(self, sorting_key: str):
        if not self._can_switch_right_view(sorting_key):
            return
        self.selected_sorting_key = sorting_key
        self.selected_view_key = sorting_key
        self.delete_btn.setEnabled(True)
        self._apply_selection_highlight()
        info = self.sorting_data_by_key.get(sorting_key)
        if info is not None:
            self.show_summary_view(info)

    def _on_section_clicked(self, section_key: str):
        info = self.section_data_by_key.get(section_key)
        if info is None:
            return
        if not self._can_switch_right_view(section_key):
            return
        sorting_key = section_key.rsplit("::", 1)[0]
        self.selected_sorting_key = sorting_key
        self.selected_view_key = section_key
        self.delete_btn.setEnabled(True)
        self._apply_selection_highlight()
        section = str(info.get("section", "")).strip().lower()
        if section == "preprocessing":
            self.show_preprocessing_view(info)
        elif section == "sorting":
            self.show_sorting_view(info)
        elif section == "curation":
            self.show_curation_view(info)

    def _apply_selection_highlight(self):
        selected = str(self.selected_view_key or "")
        clear_bg = QtGui.QBrush()
        clear_fg = QtGui.QBrush()
        selected_brush = QtGui.QBrush(QtGui.QColor("#cfe8ff"))
        # Calm, non-flashing background used only on the exact running section
        # item (for example the Sorting child row), not on the whole sorting tree.
        running_brush = QtGui.QBrush(QtGui.QColor("#eef8f0"))
        running_fg = QtGui.QBrush(QtGui.QColor("#2e7d32"))

        try:
            states = self.job_manager.poll(emit=False)
            busy_view_keys = set(states.keys())
        except Exception:
            states = {}
            busy_view_keys = set()

        if hasattr(self, "jobs_label"):
            try:
                if states:
                    labels = []
                    for view_key, state in states.items():
                        labels.append(str(state.get("text", "") or f"{state.get('section', view_key)} is running"))
                    self.jobs_label.setText("● " + ", ".join(labels))
                    self.jobs_label.setVisible(True)
                    self.jobs_label.setStyleSheet(
                        "QLabel { background: #f6faf7; color: #2e7d32; "
                        "border: 1px solid #cfe8d2; border-radius: 4px; padding: 3px 5px; }"
                    )
                else:
                    self.jobs_label.setVisible(False)
            except Exception:
                pass

        it = QtWidgets.QTreeWidgetItemIterator(self.tree)
        while it.value():
            item = it.value()
            key = str(self._user_role_value(item) or "")
            is_running_section = key in busy_view_keys
            if is_running_section:
                item.setBackground(0, running_brush)
                item.setForeground(0, running_fg)
                tip = ""
                try:
                    tip = self.job_manager.tooltip_for_key(key)
                except Exception:
                    pass
                item.setToolTip(0, tip or "This section has a background job running.")
            else:
                item.setForeground(0, clear_fg)
                item.setToolTip(0, "")
                item.setBackground(0, selected_brush if key == selected else clear_bg)
            it += 1

    def delete_spike_sorting(self):
        sorting_key = self.selected_sorting_key
        if not sorting_key:
            QtWidgets.QMessageBox.information(self, "No sorting selected", "Please select a spike sorting to delete.")
            return
        info = self.sorting_data_by_key.get(sorting_key)
        if info is None:
            QtWidgets.QMessageBox.information(self, "No sorting selected", "Please select a spike sorting to delete.")
            self.selected_sorting_key = None
            self.refresh_sorting_list()
            return
        if self._is_sorting_busy(sorting_key):
            QtWidgets.QMessageBox.warning(
                self,
                "Spike sorting is busy",
                "This spike sorting still has a running job. Please wait until it finishes before deleting it.",
            )
            return

        name = info.get("name", "")
        sorting_path = info.get("path", None)
        ok = QtWidgets.QMessageBox.question(
            self,
            "Delete spike sorting",
            f"Delete spike sorting '{name}'?\n\nThis cannot be undone.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if ok != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            if sorting_path is not None:
                root = self.sorting_root.resolve()
                target = Path(sorting_path).resolve()
                if root not in target.parents:
                    raise RuntimeError(f"Refusing to delete folder outside spike sorting root:\n{target}")
                if target.exists() and target.is_dir():
                    shutil.rmtree(target)
            self._remove_cached_views_for_sorting(sorting_key)
            self.selected_sorting_key = None
            self.selected_view_key = None
            self._current_right_view = None
            self._current_right_view_key = None
            self.refresh_sorting_list()
            self.delete_btn.setEnabled(False)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Delete failed", str(exc))

    def set_target_paths(self, paths):
        self.selected_file_list = list(dict.fromkeys(str(p) for p in (paths or []) if p))
        if self.new_sorting_dialog is not None:
            try:
                self.new_sorting_dialog.update_files(self.selected_file_list)
            except Exception:
                pass

    def open_new_sorting_dialog(self):
        if self.new_sorting_dialog is not None and self.new_sorting_dialog.isVisible():
            self.new_sorting_dialog.raise_()
            self.new_sorting_dialog.activateWindow()
            self.new_sorting_dialog.update_files(self.selected_file_list)
            return
        self.new_sorting_dialog = NewSpikeSortingDialog(
            self,
            files=self.selected_file_list,
            subject_lookup=self._subject_for_path,
            on_confirm=self._on_new_sorting_confirmed,
            project_path=self._get_project_path(),
            GUI_instance=self.GUI_instance,
        )
        self.new_sorting_dialog.finished.connect(lambda *_: setattr(self, "new_sorting_dialog", None))
        self.new_sorting_dialog.show()

    def _get_project_path(self):
        gui = self.GUI_instance
        if gui is not None and hasattr(gui, "project_root"):
            return gui.project_root
        return None

    def _on_new_sorting_confirmed(self, config: dict):
        sorting_path = self.sorting_root / config["name"]
        sorting_key = self._sorting_key(config["name"], sorting_path, False)
        self.selected_sorting_key = sorting_key
        self.selected_view_key = sorting_key
        self.refresh_sorting_list()
        info = self.sorting_data_by_key.get(sorting_key)
        if info is not None:
            self.show_summary_view(info)

    def _subject_for_path(self, path: str) -> str:
        gui = self.GUI_instance
        if gui is None or not hasattr(gui, "df"):
            return ""
        df = getattr(gui, "df", None)
        if df is None or getattr(df, "empty", True) or "file_path" not in df.columns:
            return ""
        hit = df[df["file_path"].astype(str) == str(path)]
        if hit.empty:
            return ""
        for col in ("subject_id", "subject", "animal", "mouse_id"):
            if col in hit.columns:
                value = hit.iloc[0].get(col)
                if value is not None and str(value) != "nan":
                    return str(value)
        return ""

    def _is_right_view_busy(self, view) -> bool:
        """Return True while a right-panel view owns a running worker/thread."""
        if view is None:
            return False

        checker = getattr(view, "is_busy", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                pass

        try:
            if bool(getattr(view, "_busy", False)):
                return True
        except Exception:
            pass

        for attr in ("_loader_thread", "_thread"):
            thread = getattr(view, attr, None)
            if thread is not None:
                try:
                    if thread.isRunning():
                        return True
                except Exception:
                    pass
        return False

    def _sorting_key_from_view_key(self, key: str | None) -> str:
        key = str(key or "")
        if "::" in key:
            return key.rsplit("::", 1)[0]
        return key

    def _is_sorting_busy(self, sorting_key: str) -> bool:
        sorting_key = str(sorting_key or "")
        for key, view in list(getattr(self, "_right_view_cache", {}).items()):
            if self._sorting_key_from_view_key(key) == sorting_key and self._is_right_view_busy(view):
                return True
        return False

    def _append_background_note(self, view, msg: str):
        append_log = getattr(view, "_append_log", None)
        if callable(append_log):
            try:
                append_log("")
                append_log(msg)
            except Exception:
                pass
        status_label = getattr(view, "status_label", None)
        if status_label is not None:
            try:
                status_label.setText(msg)
            except Exception:
                pass

    def _can_switch_right_view(self, target_key: str) -> bool:
        # Switching is allowed while a job is running because right-panel views
        # are cached and hidden instead of being deleted. This preserves
        # QThread/PyQtGraph object lifetime and avoids the hard kernel crash.
        # Do not overwrite the running view status label here: the sorter progress
        # text such as "Running sorting... recording 1/2" must remain visible.
        return True

    def _hide_cached_views(self, keep_key: str | None = None):
        keep_key = str(keep_key or "")
        for key, view in list(getattr(self, "_right_view_cache", {}).items()):
            if view is None:
                continue
            try:
                view.setVisible(str(key) == keep_key)
            except RuntimeError:
                # Qt object was already deleted externally; forget it.
                self._right_view_cache.pop(key, None)
            except Exception:
                pass

    def _drop_stale_cached_views(self, valid_view_keys: set):
        valid_view_keys = {str(k) for k in (valid_view_keys or set())}
        for key, view in list(getattr(self, "_right_view_cache", {}).items()):
            if str(key) in valid_view_keys:
                continue
            if self._is_right_view_busy(view):
                # Do not destroy a running view even if the tree was refreshed.
                continue
            self._right_view_cache.pop(key, None)
            try:
                view.setParent(None)
                view.deleteLater()
            except Exception:
                pass

        try:
            self.job_manager.poll()
        except Exception:
            pass

    def _remove_cached_views_for_sorting(self, sorting_key: str):
        sorting_key = str(sorting_key or "")
        for key, view in list(getattr(self, "_right_view_cache", {}).items()):
            if self._sorting_key_from_view_key(key) != sorting_key:
                continue
            if self._is_right_view_busy(view):
                continue
            self._right_view_cache.pop(key, None)
            try:
                view.setParent(None)
                view.deleteLater()
            except Exception:
                pass

        try:
            self.job_manager.poll()
        except Exception:
            pass

    def _show_right_view(self, view_class, info: dict):
        if self.right_panel is None:
            return

        target_key = str(self.selected_view_key or "")
        if not target_key:
            target_key = str(info.get("section", "summary") or "summary")

        self.right_panel.show_spike_sorting_view()
        layout = self.right_panel.spike_sorting_container_layout
        container = self.right_panel.spike_sorting_container

        cache = getattr(self, "_right_view_cache", None)
        if cache is None:
            self._right_view_cache = {}
            cache = self._right_view_cache

        view = cache.get(target_key)
        if view is None or not isinstance(view, view_class):
            # If a stale/different widget somehow exists for this key, remove it
            # only when it is not busy.
            if view is not None and not self._is_right_view_busy(view):
                try:
                    view.setParent(None)
                    view.deleteLater()
                except Exception:
                    pass
            view = view_class(info, container)
            cache[target_key] = view
            layout.addWidget(view)
            try:
                self.job_manager.attach_view(target_key, view)
            except Exception:
                pass
        else:
            # Some views implement their own refresh methods; use them if present,
            # otherwise keep the existing widget state/log/thread intact.
            for meth_name in ("set_info", "update_info", "refresh_from_info"):
                meth = getattr(view, meth_name, None)
                if callable(meth):
                    try:
                        meth(info)
                    except Exception:
                        pass
                    break

        try:
            self.job_manager.attach_view(target_key, view)
            self.job_manager.poll()
        except Exception:
            pass

        self._hide_cached_views(target_key)
        try:
            view.setVisible(True)
            view.raise_()
        except Exception:
            pass

        self._current_right_view = view
        self._current_right_view_key = target_key
        self._apply_selection_highlight()

    def show_summary_view(self, info: dict):
        self._show_right_view(SpikeSortingSummaryView, info)

    def show_preprocessing_view(self, info: dict):
        self._show_right_view(SpikeSortingPreprocessingView, info)

    def show_sorting_view(self, info: dict):
        self._show_right_view(SpikeSortingSortingView, info)

    def show_curation_view(self, info: dict):
        self._show_right_view(SpikeSortingCurationView, info)

    def show_details_view(self):
        if self.right_panel is not None:
            self.right_panel.show_spike_sorting_view()

    def hide_details_view(self):
        pass
