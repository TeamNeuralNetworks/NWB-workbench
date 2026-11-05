from __future__ import annotations
from GUI.project_paths import json_read as _pp_json_read, read_excel as _pp_read_excel
from GUI.project_tabs import TAB_DEFINITIONS, read_enabled_tabs, create_panel

import os
import sys
from pathlib import Path

import pandas as pd

from pynwb.image import ImageSeries
from pynwb.misc import AnnotationSeries
from pynwb.epoch import TimeIntervals
from pynwb.behavior import BehavioralEvents, BehavioralEpochs
from pynwb.ecephys import EventDetection

from GUI.qt_compat import QtCore, QtGui, QtWidgets
from GUI.events import EventManager
from GUI.view_state import ViewState
from GUI.panels.file_tree_panel import FileTreePanel
from GUI.panels.content_panel import ContentPanel
from GUI.panels.content_object_manager import ContentObjectManager, unit_project_path_segment
from GUI.panels.right_panel import RightPanel
from GUI.panels.filter_panel import FilterPanel
from GUI.panels.plot_panel import PlotPanel

from Tools.General.nwb_backend import NWBBackend
from Tools.General.filter_data import filter_trial_df
from Builders.General.generate_nwb_inventory import (
    REQUIRED_INVENTORY_COLUMNS,
    migrate_inventory_to_nwb_only,
    build_trial_filter_catalog,
    inventory_path_key,
    write_inventory_atomic,
    remove_internal_nwb_work_rows,
)


class RotatedLabel(QtWidgets.QLabel):
    """QLabel that paints its text rotated vertically for collapsed panes."""

    def __init__(self, text: str = "", parent=None, angle: int = -90):
        super().__init__(text, parent)
        self.angle = int(angle)
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

    def sizeHint(self):
        base = super().sizeHint()
        return QtCore.QSize(max(22, base.height() + 6), max(40, base.width() + 8))

    def minimumSizeHint(self):
        base = super().minimumSizeHint()
        return QtCore.QSize(max(22, base.height() + 4), max(28, base.width() + 6))

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        try:
            painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing, True)
        except AttributeError:
            painter.setRenderHint(QtGui.QPainter.TextAntialiasing, True)
        painter.setFont(self.font())
        try:
            color = self.palette().color(QtGui.QPalette.ColorRole.WindowText)
        except AttributeError:
            color = self.palette().color(QtGui.QPalette.WindowText)
        painter.setPen(color)

        if self.angle < 0:
            painter.translate(0, self.height())
            painter.rotate(-90)
        else:
            painter.translate(self.width(), 0)
            painter.rotate(90)

        rect = QtCore.QRect(0, 0, self.height(), self.width())
        painter.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter, self.text())

class CollapsiblePane(QtWidgets.QFrame):
    """Small wrapper that lets a QSplitter panel collapse to a thin handle.

    The wrapped widget is hidden when collapsed, then restored with its previous
    splitter width when expanded. This is useful when the right plot/summary
    view needs more horizontal space.
    """

    def __init__(self, title: str, content_widget: QtWidgets.QWidget, splitter: QtWidgets.QSplitter,
                 collapsed_width: int = 30, default_width: int = 220, parent=None):
        super().__init__(parent)
        self.title = str(title)
        self.content_widget = content_widget
        self._splitter = splitter
        self._collapsed_width = int(collapsed_width)
        self._last_expanded_width = int(default_width)
        self._collapsed = False

        try:
            self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        except AttributeError:
            self.setFrameShape(QtWidgets.QFrame.StyledPanel)

        self._outer_layout = QtWidgets.QVBoxLayout(self)
        self._outer_layout.setContentsMargins(0, 0, 0, 0)
        self._outer_layout.setSpacing(0)

        # Two separate headers are used. Do not put them in a QStackedWidget:
        # a stacked widget reserves the maximum height of both pages, so the
        # vertical collapsed title would make the expanded header very tall.
        self._expanded_header = QtWidgets.QWidget(self)
        self._expanded_header_layout = QtWidgets.QHBoxLayout(self._expanded_header)
        self._expanded_header_layout.setContentsMargins(3, 2, 3, 2)
        self._expanded_header_layout.setSpacing(4)

        self.collapse_btn = QtWidgets.QToolButton(self._expanded_header)
        self.collapse_btn.setText("\u2212")
        self.collapse_btn.setToolTip(f"Collapse {self.title}")
        self.collapse_btn.clicked.connect(self.toggle_collapsed)
        self._expanded_header_layout.addWidget(self.collapse_btn)

        self.title_lbl = QtWidgets.QLabel(self.title, self._expanded_header)
        self.title_lbl.setStyleSheet("font-weight: 600;")
        self._expanded_header_layout.addWidget(self.title_lbl)
        self._expanded_header_layout.addStretch(1)

        self._collapsed_header = QtWidgets.QWidget(self)
        self._collapsed_header_layout = QtWidgets.QVBoxLayout(self._collapsed_header)
        self._collapsed_header_layout.setContentsMargins(2, 3, 2, 3)
        self._collapsed_header_layout.setSpacing(2)

        self.collapsed_title_lbl = RotatedLabel(self.title, self._collapsed_header)
        self.collapsed_title_lbl.setStyleSheet("font-weight: 600; font-size: 9pt;")
        # Keep the collapsed label at its natural rotated size. Do not let it
        # expand vertically, otherwise the text is centered inside a large blank
        # area instead of sitting just above the + button.
        self.collapsed_title_lbl.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Fixed
            if hasattr(QtWidgets.QSizePolicy, "Policy")
            else QtWidgets.QSizePolicy.Fixed,
            QtWidgets.QSizePolicy.Policy.Fixed
            if hasattr(QtWidgets.QSizePolicy, "Policy")
            else QtWidgets.QSizePolicy.Fixed,
        )

        self.expand_btn = QtWidgets.QToolButton(self._collapsed_header)
        self.expand_btn.setText("+")
        self.expand_btn.setToolTip(f"Expand {self.title}")
        self.expand_btn.clicked.connect(self.toggle_collapsed)

        # Center the collapsed controls as one compact block, with the vertical
        # title directly above the + button.
        self._collapsed_header_layout.addStretch(1)
        self._collapsed_header_layout.addWidget(self.collapsed_title_lbl, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)
        self._collapsed_header_layout.addWidget(self.expand_btn, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)
        self._collapsed_header_layout.addStretch(1)

        self._expanded_header.setVisible(True)
        self._collapsed_header.setVisible(False)

        # Backward-compatible name in case another panel accesses the button.
        self.toggle_btn = self.collapse_btn

        # Vertical title keeps the collapsed strip narrow.
        self._collapsed_width = max(self._collapsed_width, 30)

        self._outer_layout.addWidget(self._expanded_header, 0)
        self._outer_layout.addWidget(self._collapsed_header, 0)
        self._outer_layout.addWidget(self.content_widget, 1)

    def is_collapsed(self) -> bool:
        return bool(self._collapsed)

    def toggle_collapsed(self):
        self.set_collapsed(not self._collapsed)

    def set_collapsed(self, collapsed: bool):
        collapsed = bool(collapsed)
        if collapsed == self._collapsed:
            return

        idx = self._splitter.indexOf(self) if self._splitter is not None else -1
        sizes = self._splitter.sizes() if self._splitter is not None else []

        if collapsed:
            if idx >= 0 and idx < len(sizes):
                self._last_expanded_width = max(int(sizes[idx]), self._last_expanded_width, self._collapsed_width + 1)

            self._collapsed = True
            self.content_widget.setVisible(False)
            self._expanded_header.setVisible(False)
            self._collapsed_header.setVisible(True)
            self.setMinimumWidth(self._collapsed_width)
            self.setMaximumWidth(self._collapsed_width)

            if idx >= 0 and idx < len(sizes):
                freed = max(0, int(sizes[idx]) - self._collapsed_width)
                sizes[idx] = self._collapsed_width
                target = self._best_space_target(idx, sizes)
                if target is not None:
                    sizes[target] = max(1, int(sizes[target]) + freed)
                self._splitter.setSizes(sizes)
            return

        self._collapsed = False
        self.setMaximumWidth(16777215)
        self.setMinimumWidth(0)
        self.content_widget.setVisible(True)
        self._collapsed_header.setVisible(False)
        self._expanded_header.setVisible(True)

        if idx >= 0 and idx < len(sizes):
            wanted = max(self._last_expanded_width, self._collapsed_width + 80)
            current = int(sizes[idx])
            delta = max(0, wanted - current)
            target = self._best_space_target(idx, sizes)
            if target is not None and delta > 0:
                take = min(delta, max(0, int(sizes[target]) - 80))
                sizes[target] = max(80, int(sizes[target]) - take)
                sizes[idx] = current + take
            else:
                sizes[idx] = max(current, wanted)
            self._splitter.setSizes(sizes)

    def _best_space_target(self, idx: int, sizes: list[int]):
        if not sizes:
            return None
        # Prefer giving/taking space from the right-most visible panel, since it
        # is usually the plot/summary area. If the right-most panel itself is
        # being collapsed/expanded, use its left neighbour.
        for target in range(len(sizes) - 1, -1, -1):
            if target != idx:
                return target
        return None


class NWBApp(QtWidgets.QMainWindow):
    """First Qt/PySide migration of the NWB GUI shell.

    This milestone ports the stable app skeleton plus file tree, content tree,
    trials table, file-list view, and data plotting with native PyQtGraph.
    Processing, Analysis, and Spike sorting are intentionally left as Qt
    placeholders so they can be migrated one panel at a time without breaking
    the current Tk GUI.
    """

    def __init__(self, project_root, parent=None):
        super().__init__(parent)
        self._startup_foreground_scheduled = False
        self._startup_native_topmost = False
        self.project_root = Path(project_root).resolve()
        from GUI.project_paths import configure_project_context
        configure_project_context(self.project_root)
        self.NWB_inventory_path = self.project_root / "Data" / "nwb_inventory.xlsx"

        self.df, self.content_df, self.trial_filter_catalog_df = self._load_or_init_inventory()
        self._content_df_master = self.content_df.copy(deep=True)
        self._df_master = self.df.copy(deep=True)
        self.full_df = self.df.copy()
        self.current_trial_filters = {}
        self.selected_file_list = []

        # services
        self.backend = NWBBackend()
        self.events = EventManager()
        self.view_state = ViewState(window_s=5.0)
        self.probe_viewer = None

        # content maps
        self.trials_nodes_map = {}
        self.data_nodes_map = {}
        self.managed_object_nodes_map = {}
        self.event_nodes_map = {}
        self.folder_leaf_files_map = {}
        self.folder_leaf_object_map = {}
        self._current_plotted_object_context = None

        self.setWindowTitle("NWB Workbench")
        self.resize(1900, 900)
        self._setup_application_icon()

        self._build_ui()
        self.file_tree_panel.build_tree()
        self._refresh_empty_project_ui(select_processing_if_empty=True)

    def showEvent(self, event):
        """Bring the main application to the front once its window is visible."""
        super().showEvent(event)
        if self._startup_foreground_scheduled:
            return
        self._startup_foreground_scheduled = True

        # Waiting until the event loop resumes ensures that Qt has created a
        # valid native HWND.  A second activation covers delayed Windows paint
        # and focus events during the first layout pass.
        QtCore.QTimer.singleShot(0, self._bring_startup_window_to_front)
        QtCore.QTimer.singleShot(100, self._request_startup_window_activation)
        QtCore.QTimer.singleShot(350, self._release_startup_window_topmost)

    def _request_startup_window_activation(self):
        """Raise and focus the already visible main window through Qt."""
        try:
            self.raise_()
        except Exception:
            pass
        try:
            self.activateWindow()
        except Exception:
            pass
        try:
            handle = self.windowHandle()
            if handle is not None and hasattr(handle, "requestActivate"):
                handle.requestActivate()
        except Exception:
            pass

    def _bring_startup_window_to_front(self):
        """Temporarily put NWB Workbench above every other Windows window."""
        self._request_startup_window_activation()
        if not sys.platform.startswith("win"):
            return

        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            hwnd = wintypes.HWND(int(self.winId()))

            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.ShowWindow.restype = wintypes.BOOL
            user32.SetWindowPos.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            ]
            user32.SetWindowPos.restype = wintypes.BOOL
            user32.BringWindowToTop.argtypes = [wintypes.HWND]
            user32.BringWindowToTop.restype = wintypes.BOOL
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            user32.SetForegroundWindow.restype = wintypes.BOOL

            sw_restore = 9
            swp_no_move = 0x0002
            swp_no_size = 0x0001
            swp_show_window = 0x0040
            flags = swp_no_move | swp_no_size | swp_show_window

            user32.ShowWindow(hwnd, sw_restore)
            user32.SetWindowPos(
                hwnd,
                wintypes.HWND(-1),  # HWND_TOPMOST
                0,
                0,
                0,
                0,
                flags,
            )
            self._startup_native_topmost = True
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        except Exception:
            # Qt activation above remains the safe fallback. Startup focus must
            # never prevent the application from opening.
            self._startup_native_topmost = False

    def _release_startup_window_topmost(self):
        """Return to normal stacking without giving focus back to another app."""
        if sys.platform.startswith("win") and self._startup_native_topmost:
            try:
                import ctypes
                from ctypes import wintypes

                user32 = ctypes.windll.user32
                hwnd = wintypes.HWND(int(self.winId()))
                user32.SetWindowPos.argtypes = [
                    wintypes.HWND,
                    wintypes.HWND,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    wintypes.UINT,
                ]
                user32.SetWindowPos.restype = wintypes.BOOL
                user32.SetForegroundWindow.argtypes = [wintypes.HWND]
                user32.SetForegroundWindow.restype = wintypes.BOOL
                swp_no_move = 0x0002
                swp_no_size = 0x0001
                swp_show_window = 0x0040
                flags = swp_no_move | swp_no_size | swp_show_window
                user32.SetWindowPos(
                    hwnd,
                    wintypes.HWND(-2),  # HWND_NOTOPMOST
                    0,
                    0,
                    0,
                    0,
                    flags,
                )
                user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
            finally:
                self._startup_native_topmost = False

        self._request_startup_window_activation()

    def _setup_application_icon(self):
        from GUI.application_icon import install_window_icon
        install_window_icon(self,self.project_root)

    # ---------------- inventory ----------------

    def _load_or_init_inventory(self):
        migrate_inventory_to_nwb_only(self.NWB_inventory_path)
        if self.NWB_inventory_path.exists():
            try:
                df = _pp_read_excel(self.NWB_inventory_path, sheet_name="inventory")
                df = remove_internal_nwb_work_rows(df)
            except Exception:
                df = pd.DataFrame(columns=list(REQUIRED_INVENTORY_COLUMNS))
            try:
                content_df = _pp_read_excel(self.NWB_inventory_path, sheet_name="content_index")
                content_df = remove_internal_nwb_work_rows(content_df)
            except Exception:
                content_df = pd.DataFrame()
            try:
                trial_filter_catalog_df = _pp_read_excel(self.NWB_inventory_path, sheet_name="trial_filter_catalog")
            except Exception:
                trial_filter_catalog_df = pd.DataFrame()
            return df, content_df, trial_filter_catalog_df

        df = pd.DataFrame(columns=list(REQUIRED_INVENTORY_COLUMNS))
        return df, pd.DataFrame(), pd.DataFrame()

    def _has_inventory_file_data(self) -> bool:
        df = getattr(self, "df", None)
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return False
        if "file_path" not in df.columns:
            return False
        try:
            paths = df["file_path"].dropna().astype(str).str.strip()
            return bool((paths != "").any())
        except Exception:
            return False

    def _refresh_empty_project_ui(self, *, select_processing_if_empty: bool = False):
        """Show a clear first-use message when the project has no inventory yet."""
        has_data = self._has_inventory_file_data()

        if not has_data:
            tree = getattr(getattr(self, "file_tree_panel", None), "tree", None)
            if tree is not None:
                try:
                    tree.clear()
                    item = QtWidgets.QTreeWidgetItem(["No data please scan / update data inventory"])
                    try:
                        item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable)
                    except AttributeError:
                        item.setFlags(item.flags() & ~QtCore.Qt.ItemIsSelectable)
                    try:
                        item.setForeground(0, QtGui.QBrush(QtGui.QColor("#555555")))
                    except Exception:
                        pass
                    tree.addTopLevelItem(item)
                    tree.expandAll()
                    if hasattr(self.file_tree_panel, "node_map"):
                        self.file_tree_panel.node_map.clear()
                    if hasattr(self.file_tree_panel, "common_root"):
                        self.file_tree_panel.common_root = None
                except Exception:
                    pass

            if select_processing_if_empty and hasattr(self, "middle_notebook") and self.processing_panel is not None:
                try:
                    index = self.middle_notebook.indexOf(self.processing_panel)
                    if index >= 0:
                        self.middle_notebook.setCurrentIndex(index)
                except Exception:
                    pass

        return has_data

    def reload_inventory(self):
        migrate_inventory_to_nwb_only(self.NWB_inventory_path)
        if self.NWB_inventory_path.exists():
            try:
                self.df = _pp_read_excel(self.NWB_inventory_path, sheet_name="inventory")
                self.df = remove_internal_nwb_work_rows(self.df)
            except Exception:
                self.df = pd.DataFrame(columns=list(REQUIRED_INVENTORY_COLUMNS))
            try:
                self.content_df = _pp_read_excel(self.NWB_inventory_path, sheet_name="content_index")
                self.content_df = remove_internal_nwb_work_rows(self.content_df)
            except Exception:
                self.content_df = pd.DataFrame()
            try:
                self.trial_filter_catalog_df = _pp_read_excel(self.NWB_inventory_path, sheet_name="trial_filter_catalog")
            except Exception:
                self.trial_filter_catalog_df = pd.DataFrame()
        else:
            self.df = pd.DataFrame(columns=list(REQUIRED_INVENTORY_COLUMNS))
            self.content_df = pd.DataFrame()
            self.trial_filter_catalog_df = pd.DataFrame()

        self.full_df = self.df.copy()
        self._df_master = self.df.copy(deep=True)
        self._content_df_master = self.content_df.copy(deep=True)
        self.filter_panel.rebuild_from_inventory()
        self.file_tree_panel.df = self.df
        self.file_tree_panel.build_tree()
        self._refresh_empty_project_ui(select_processing_if_empty=True)

    def _add_inventory_files(self):
        self.file_tree_panel.add_btn.setEnabled(False)
        try:
            from Builders.General.scan_update_data_inventory import run_scan_update_inventory

            if run_scan_update_inventory(self):
                self.apply_filtered_inventory(self.df)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Add NWB files failed", str(exc))
        finally:
            self.file_tree_panel.add_btn.setEnabled(True)

    def _delete_inventory_files(self):
        paths = self.file_tree_panel.selected_inventory_paths()
        if not paths:
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "Remove files from project",
            f"Remove {len(paths)} selected file(s) from this project inventory?\n\n"
            "The NWB files on disk will be kept.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        self.file_tree_panel.delete_btn.setEnabled(False)
        try:
            # Read the complete workbook, rather than the filtered visible table.
            sheets = _pp_read_excel(self.NWB_inventory_path, sheet_name=None)
            keys = {inventory_path_key(path) for path in paths}
            for name, dataframe in sheets.items():
                if "file_path" in dataframe.columns:
                    remove = dataframe["file_path"].map(
                        lambda value: inventory_path_key(value) in keys
                        if pd.notna(value) else False
                    )
                    sheets[name] = dataframe.loc[~remove].reset_index(drop=True)
            sheets["trial_filter_catalog"] = build_trial_filter_catalog(
                sheets.get("trial_filter_index", pd.DataFrame())
            )
            write_inventory_atomic(self.NWB_inventory_path, sheets)
            self.reload_inventory()
            self.apply_filtered_inventory(self.df)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Remove files failed", str(exc))
        finally:
            self.file_tree_panel.delete_btn.setEnabled(
                bool(self.file_tree_panel.selected_inventory_paths())
            )

    # ---------------- UI ----------------

    def _build_ui(self):
        try:
            horizontal = QtCore.Qt.Orientation.Horizontal
        except AttributeError:
            horizontal = QtCore.Qt.Horizontal
        splitter = QtWidgets.QSplitter(horizontal)
        splitter.setChildrenCollapsible(True)
        self.main_splitter = splitter
        self.setCentralWidget(splitter)

        # far left filters
        self.filter_panel = FilterPanel(self)
        self.filter_pane = CollapsiblePane("Filters", self.filter_panel, splitter, default_width=250)
        splitter.addWidget(self.filter_pane)

        # file tree
        self.file_tree_panel = FileTreePanel(self.df, on_select=self.on_select)
        self.file_tree_panel.add_requested.connect(self._add_inventory_files)
        self.file_tree_panel.delete_requested.connect(self._delete_inventory_files)
        self.file_tree_pane = CollapsiblePane("Files", self.file_tree_panel, splitter, default_width=300)
        splitter.addWidget(self.file_tree_pane)

        # middle notebook
        self.middle_notebook = QtWidgets.QTabWidget()
        self.middle_pane = CollapsiblePane("Workspace", self.middle_notebook, splitter, default_width=360)
        splitter.addWidget(self.middle_pane)

        self.content_panel = ContentPanel(on_item_click=self.on_content_item_click)
        self.content_panel.tree.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.content_panel.tree.customContextMenuRequested.connect(self._content_view_menu)
        self.content_panel.running_view_requested.connect(
            self._show_active_content_object_operation
        )

        # Create the right panel before panels that need a reference to it.
        # It is added to the splitter below so the visual order is unchanged.
        self.right_panel = RightPanel()

        self.content_object_manager = ContentObjectManager(
            GUI_instance=self,
            parent=self.right_panel.content_manager_container,
        )
        self.content_object_manager.operation_finished.connect(self._on_nwb_object_edit_finished)
        self.content_object_manager.plot_requested.connect(self._plot_managed_object)
        self.content_object_manager.runningStateChanged.connect(
            self._on_nwb_object_running_state_changed
        )
        self.right_panel.set_content_manager_widget(self.content_object_manager)

        enabled_tabs = set(read_enabled_tabs(self.project_root))
        self._workflow_tabs = []
        self.middle_notebook.addTab(self.content_panel, "Content")
        for definition in TAB_DEFINITIONS:
            setattr(self, definition["attribute"], None)
            if definition["id"] not in enabled_tabs:
                continue
            panel = create_panel(definition, right_panel=self.right_panel, GUI_instance=self)
            setattr(self, definition["attribute"], panel)
            self._workflow_tabs.append(definition)
            self.middle_notebook.addTab(panel, definition["title"])
        self.middle_notebook.currentChanged.connect(self._on_middle_tab_changed)
        self._connect_running_tab_indicators()

        # right panel
        self.right_pane = CollapsiblePane("Right view", self.right_panel, splitter, default_width=1030)
        splitter.addWidget(self.right_pane)

        # Keep Filters compact and reserve just enough width for every tab.
        # Give the remaining initial width to the right-hand view.
        self.middle_notebook.ensurePolished()
        tabs_width = self.middle_notebook.tabBar().sizeHint().width() + 8
        self.middle_notebook.setMinimumWidth(tabs_width)
        workspace_width = tabs_width
        filters_width = 250
        files_width = 300
        right_width = max(0, 1800 - filters_width - files_width - workspace_width)
        self.middle_pane._last_expanded_width = workspace_width
        splitter.setSizes([filters_width, files_width, workspace_width, right_width])
        # Keep the tab column at its requested width when the window grows.
        # The user can still resize it explicitly with the splitter handle.
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 0)
        splitter.setStretchFactor(2, 0)
        splitter.setStretchFactor(3, 1)

        self.plot_panel = PlotPanel(
            right_panel=self.right_panel,
            backend=self.backend,
            events=self.events,
            view_state=self.view_state,
            probe_viewer=self.probe_viewer,
            manage_object_callback=self._show_current_plotted_object_manager,
        )

        # No toolbar button here: keep milestone 1 closer to the Tk GUI.
        # reload_inventory() remains available programmatically if the inventory file is regenerated.

    def enable_deeplabcut_tab(self):
        from pathlib import Path
        import json
        from GUI.project_tabs import PROJECT_METADATA_NAME
        from GUI.panels.deeplabcut_panel import save_json
        try:
            panel = self.deeplabcut_panel
            if panel is None:
                definition = next(t for t in TAB_DEFINITIONS if t['id'] == 'deeplabcut')
                metadata_path = Path(self.project_root) / PROJECT_METADATA_NAME
                metadata = _pp_json_read(metadata_path) if metadata_path.exists() else {}
                metadata['enabled_tabs'] = list(dict.fromkeys(read_enabled_tabs(self.project_root) + ['deeplabcut']))
                panel = create_panel(definition, right_panel=self.right_panel, GUI_instance=self)
                save_json(metadata_path, metadata)
                self.deeplabcut_panel = panel
                self._workflow_tabs.append(definition)
                ranks = {item['title']: i for i, item in enumerate(TAB_DEFINITIONS)}
                position = next((i for i in range(self.middle_notebook.count())
                                 if ranks.get(self.middle_notebook.tabText(i), -1) > ranks[definition['title']]),
                                self.middle_notebook.count())
                self.middle_notebook.insertTab(position, panel, definition['title'])
                panel.runningStateChanged.connect(lambda *_: self._update_running_tab_colors())
                panel.set_target_paths(getattr(self, 'selected_file_list', []))
                self.middle_notebook.setMinimumWidth(self.middle_notebook.tabBar().sizeHint().width() + 8)
            self.middle_notebook.setCurrentWidget(panel)
            self.right_panel.show_deeplabcut_view()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, 'DeepLabCut', str(exc))

    def _connect_running_tab_indicators(self):
        for definition in self._workflow_tabs:
            panel = getattr(self, definition["attribute"], None)
            signals = [getattr(panel, "runningStateChanged", None),
                       getattr(getattr(panel, "job_manager", None), "jobsChanged", None)]
            for signal in signals:
                if signal is not None:
                    signal.connect(lambda *_args, _self=self: _self._update_running_tab_colors())

        self._running_tab_timer = QtCore.QTimer(self)
        self._running_tab_timer.setInterval(1000)
        self._running_tab_timer.timeout.connect(self._update_running_tab_colors)
        self._running_tab_timer.start()
        self._update_running_tab_colors()

    def _panel_is_running(self, attr_name: str) -> bool:
        panel = getattr(self, attr_name, None)
        if panel is None:
            return False

        is_running = getattr(panel, "is_running", None)
        if callable(is_running):
            try:
                return bool(is_running())
            except Exception:
                pass

        # Spike sorting owns a small job manager instead of a single boolean.
        job_manager = getattr(panel, "job_manager", None)
        if job_manager is not None:
            try:
                return bool(job_manager.busy_view_keys())
            except Exception:
                return False

        return False

    def _update_running_tab_colors(self):
        if not hasattr(self, "middle_notebook"):
            return

        running_by_title = {
            "Content": bool(
                getattr(getattr(self, "content_object_manager", None), "is_busy", lambda: False)()
            ),
            **{definition["title"]: self._panel_is_running(definition["attribute"])
               for definition in self._workflow_tabs},
        }

        tab_bar = self.middle_notebook.tabBar()
        green = QtGui.QColor("#2e7d32")
        default_color = QtGui.QColor()

        for i in range(self.middle_notebook.count()):
            title = self.middle_notebook.tabText(i)
            try:
                tab_bar.setTabTextColor(i, green if running_by_title.get(title, False) else default_color)
            except Exception:
                pass

    def _on_middle_tab_changed(self, index: int):
        try:
            tab_name = self.middle_notebook.tabText(index)
        except Exception:
            tab_name = ""
        self.filter_panel.set_trial_filters_visible(tab_name == "Analysis")

        if hasattr(self, "right_panel"):
            try:
                if tab_name == "Content":
                    manager = getattr(self, "content_object_manager", None)
                    if manager is not None and manager.is_busy():
                        self.right_panel.show_content_manager_view()
                else:
                    for definition in self._workflow_tabs:
                        if definition["title"] == tab_name:
                            getattr(self.right_panel, definition["right_view"])()
                            break
            except Exception:
                pass

    # ---------------- filtering ----------------

    def apply_filtered_inventory(self, filtered_df: pd.DataFrame):
        self.df = filtered_df.copy()
        self.file_tree_panel.df = self.df
        self.file_tree_panel.build_tree()
        self._refresh_empty_project_ui(select_processing_if_empty=False)
        self.content_panel.clear()
        self.folder_leaf_files_map.clear()
        self.folder_leaf_object_map.clear()
        self.trials_nodes_map.clear()
        self.data_nodes_map.clear()
        self.managed_object_nodes_map.clear()
        self.event_nodes_map.clear()
        self.clear_trials_table()
        self.selected_file_list = []
        # File filters clear the file-tree selection. Propagate that selection
        # change to every workflow without asking the panels to navigate away
        # from their currently displayed process or pipeline step.
        for definition in self._workflow_tabs:
            panel = getattr(self, definition["attribute"], None)
            if panel is not None and hasattr(panel, "set_target_paths"):
                panel.set_target_paths([])

    # ---------------- file tree selection ----------------

    def _tree_item_to_path(self, item):
        return self.file_tree_panel.item_to_path(item)

    def on_select(self):
        self.content_panel.clear()
        self.clear_trials_table()

        if not self.file_tree_panel.selected_paths():
            self.selected_file_list = []
            for definition in self._workflow_tabs:
                panel = getattr(self, definition["attribute"], None)
                if panel is not None and hasattr(panel, "set_target_paths"):
                    panel.set_target_paths([])
            return

        if self.df is None or self.df.empty or "file_path" not in self.df.columns:
            return

        selected_paths = self.file_tree_panel.selected_paths()
        if not selected_paths:
            return

        # Resolve folder descendants once, then filter the inventory in one pass.
        # Repeated startswith scans were quadratic when selecting many files.
        inventory_paths = set(self.file_tree_panel.selected_inventory_paths())
        subset = self.df[self.df["file_path"].astype(str).isin(inventory_paths)]
        subset = subset.drop_duplicates(subset=["file_path"])
        if subset.empty:
            return

        paths = list(dict.fromkeys(subset["file_path"].dropna().astype(str)))
        self.selected_file_list = paths

        for definition in self._workflow_tabs:
            panel = getattr(self, definition["attribute"], None)
            if hasattr(panel, "set_target_paths"):
                panel.set_target_paths(paths)

        if len(selected_paths) == 1:
            selected_path = selected_paths[0]
            if len(subset) == 1 and str(subset.iloc[0]["file_path"]) == str(selected_path):
                self.display_file(subset.iloc[0])
            else:
                self.display_folder_summary(selected_path, subset)
        else:
            self.display_folder_summary("Multiple selected files", subset)

    # ---------------- content display helpers ----------------

    def _insert(self, parent, text, open_=False):
        return self.content_panel.insert(parent, text, open_=open_)

    def _insert_metadata_summary(self, parent_node, subset):
        if subset is None or subset.empty or "file_path" not in subset.columns:
            return
        if "file_type" in subset.columns:
            subset = subset[subset["file_type"] == "nwb"]
        if subset.empty:
            return

        meta_root = None
        for col in subset.columns:
            if col in {"file_path", "nwb_path", "read_error", "session_description"}:
                continue
            try:
                counts = subset[col].dropna().astype(str).value_counts()
            except Exception:
                continue
            counts = counts[counts > 0]
            if counts.empty or len(counts) > 50:
                continue
            if meta_root is None:
                meta_root = self._insert(parent_node, "Metadata summary", open_=True)
            col_node = self._insert(meta_root, col, open_=False)
            for value, n in counts.items():
                self._insert(col_node, f"{value}: {int(n)} file(s)")

    def display_folder_summary(self, folder_name: str, subset: pd.DataFrame):
        self.content_panel.clear()
        self.folder_leaf_files_map.clear()
        self.folder_leaf_object_map.clear()
        self.trials_nodes_map.clear()
        self.data_nodes_map.clear()
        self.managed_object_nodes_map.clear()
        self.event_nodes_map.clear()
        summary_node = self._insert(None, f"Folder Summary: {folder_name}", open_=True)

        if "file_type" in subset.columns:
            n_nwb = int((subset["file_type"] == "nwb").sum())
            self._insert(summary_node, f"NWB files: {n_nwb}")
            self._insert_metadata_summary(summary_node, subset)

        cdf = getattr(self, "content_df", None)
        if cdf is None or cdf.empty or "file_path" not in subset.columns:
            return
        if "file_type" in subset.columns:
            folder_paths = subset.loc[subset["file_type"] == "nwb", "file_path"].dropna().astype(str).unique().tolist()
        else:
            folder_paths = subset["file_path"].dropna().astype(str).unique().tolist()
        if not folder_paths:
            return

        required = {"file_path", "nwb_path", "name", "neurodata_type"}
        if not required.issubset(cdf.columns):
            return
        cdf2 = cdf[cdf["file_path"].astype(str).isin(folder_paths)].copy()
        if cdf2.empty:
            return
        cdf2 = cdf2.dropna(subset=["file_path", "nwb_path"]).copy()
        cdf2["file_path"] = cdf2["file_path"].astype(str)
        cdf2["nwb_path"] = cdf2["nwb_path"].astype(str).str.strip().str.replace("\\", "/", regex=False)
        cdf2["name"] = cdf2["name"].astype(str)
        cdf2["neurodata_type"] = cdf2["neurodata_type"].astype(str).str.strip()

        def _is_trials_row(r):
            p = str(r["nwb_path"]).strip().lower()
            n = str(r["name"]).strip().lower()
            t = str(r["neurodata_type"]).strip().lower()
            return p == "trials" or n == "trials" or t == "trials"

        cdf2 = cdf2[~cdf2.apply(_is_trials_row, axis=1)]
        if cdf2.empty:
            return

        event_types = {
            "TimeIntervals",
            "AnnotationSeries",
            "BehavioralEvents",
            "BehavioralEpochs",
            "EventDetection",
            "EventsTable",
        }
        cdf_evt = cdf2[cdf2["neurodata_type"].isin(event_types)].copy()
        cdf_norm = cdf2[~cdf2["neurodata_type"].isin(event_types)].copy()
        root = self._insert(summary_node, "Data contained", open_=True)
        nodes = {}

        def _get_or_create(parent, key_parts, label, open_=True):
            k = tuple(key_parts)
            if k in nodes:
                return nodes[k]
            nid = self._insert(parent, label, open_=open_)
            nodes[k] = nid
            return nid

        if not cdf_norm.empty:
            counts_norm = cdf_norm.groupby(["nwb_path", "neurodata_type"])["file_path"].nunique().sort_index()
            for (path, ndt), nfiles in counts_norm.items():
                parts = [p for p in str(path).split("/") if p]
                if not parts:
                    continue
                parent = root
                key_parts = []
                for p in parts[:-1]:
                    key_parts.append(p)
                    parent = _get_or_create(parent, key_parts, p, open_=True)
                leaf = parts[-1]
                item = self._insert(parent, f"{leaf} ({int(nfiles)} files)")
                matching_rows = cdf_norm[
                    (cdf_norm["nwb_path"] == str(path))
                    & (cdf_norm["neurodata_type"] == str(ndt))
                ]
                paths_here = matching_rows["file_path"].dropna().astype(str).unique().tolist()
                sorting_project = ""
                if str(path).strip().lower().startswith("units/"):
                    for column in ("sorting_project", "module"):
                        if column not in matching_rows.columns:
                            continue
                        values = (
                            matching_rows[column]
                            .dropna()
                            .astype(str)
                            .map(str.strip)
                        )
                        values = values[values.str.lower() != "nan"]
                        if not values.empty:
                            sorting_project = str(values.iloc[0])
                            break
                self.folder_leaf_files_map[id(item)] = paths_here
                self.folder_leaf_object_map[id(item)] = {
                    "nwb_path": str(path),
                    "neurodata_type": str(ndt),
                    "file_paths": paths_here,
                    "sorting_project": sorting_project,
                }

        if not cdf_evt.empty:
            evt_root = self._insert(root, "Event markers", open_=True)
            counts_evt = cdf_evt.groupby(["nwb_path", "neurodata_type"])["file_path"].nunique().sort_index()
            for (path, ndt), nfiles in counts_evt.items():
                parts = [p for p in str(path).split("/") if p]
                label = parts[-1] if parts else str(path)
                item = self._insert(evt_root, f"{label} ({int(nfiles)} files)")
                paths_here = cdf_evt[(cdf_evt["nwb_path"] == str(path)) & (cdf_evt["neurodata_type"] == str(ndt))]["file_path"].dropna().astype(str).unique().tolist()
                self.folder_leaf_files_map[id(item)] = paths_here
                self.folder_leaf_object_map[id(item)] = {
                    "nwb_path": str(path),
                    "neurodata_type": str(ndt),
                    "file_paths": paths_here,
                }

    def display_file(self, row: pd.Series):
        self.content_panel.clear()
        self.clear_trials_table()
        self.folder_leaf_files_map.clear()
        self.folder_leaf_object_map.clear()
        self.trials_nodes_map.clear()
        self.data_nodes_map.clear()
        self.managed_object_nodes_map.clear()
        self.event_nodes_map.clear()

        nwb_path = str(row.get("file_path", ""))

        meta_node = self._insert(None, "Metadata", open_=True)
        for col in ["identifier", "session_description", "session_start_time", "subject_id", "species", "genotype", "sex"]:
            self._insert(meta_node, f"{col}: {row.get(col)}")

        if not nwb_path or not os.path.exists(nwb_path):
            self._insert(None, "File path does not exist on disk.")
            return

        try:
            nwbfile = self.backend.open(nwb_path)
        except Exception as exc:
            self._insert(None, f"Could not open NWB file: {exc}")
            return

        if getattr(nwbfile, "trials", None) is not None:
            trials_node = self._insert(None, "Trials", open_=False)
            self.trials_nodes_map[id(trials_node)] = nwb_path
            try:
                n_trials = len(nwbfile.trials.id[:])
            except Exception:
                try:
                    n_trials = len(nwbfile.trials.to_dataframe())
                except Exception:
                    n_trials = row.get("number_of_trials")
            self._insert(trials_node, f"number_of_trials: {n_trials}")

        # Acquisition
        if getattr(nwbfile, "acquisition", None):
            node = None
            for k, obj in nwbfile.acquisition.items():
                if node is None:
                    node = self._insert(None, "Acquisition", open_=True)
                child = self._insert(node, str(k) + (" (video)" if isinstance(obj, ImageSeries) else ""))
                if isinstance(obj, AnnotationSeries):
                    self.managed_object_nodes_map[id(child)] = {
                        "file_path": nwb_path,
                        "object_path": f"acquisition/{k}",
                        "neurodata_type": obj.__class__.__name__,
                        "plot_capable": False,
                    }
                else:
                    self.data_nodes_map[id(child)] = (nwb_path, "acquisition", str(k))

        # Processing modules
        if getattr(nwbfile, "processing", None):
            proc_root = None
            for mod_name, mod in nwbfile.processing.items():
                mod_node = None
                for k, obj in getattr(mod, "data_interfaces", {}).items():
                    if proc_root is None:
                        proc_root = self._insert(None, "Processing Modules", open_=True)
                    if mod_node is None:
                        mod_node = self._insert(proc_root, str(mod_name), open_=True)
                    if isinstance(obj, (BehavioralEvents, BehavioralEpochs, EventDetection)):
                        child = self._insert(mod_node, str(k))
                        self.managed_object_nodes_map[id(child)] = {
                            "file_path": nwb_path,
                            "object_path": f"processing/{mod_name}/{k}",
                            "neurodata_type": obj.__class__.__name__,
                            "plot_capable": False,
                        }
                        # Multi-container event interfaces can also contain
                        # independently stored complete TimeSeries objects.
                        for collection_name in ("time_series", "interval_series"):
                            collection = getattr(obj, collection_name, None)
                            if not collection:
                                continue
                            try:
                                child_items = list(collection.items())
                            except Exception:
                                child_items = []
                            for event_name, event_object in child_items:
                                event_item = self._insert(child, str(event_name))
                                self.managed_object_nodes_map[id(event_item)] = {
                                    "file_path": nwb_path,
                                    "object_path": f"processing/{mod_name}/{k}/{event_name}",
                                    "neurodata_type": event_object.__class__.__name__,
                                    "plot_capable": False,
                                }
                        continue
                    is_timeseries_like = hasattr(obj, "data") and (hasattr(obj, "rate") or hasattr(obj, "timestamps"))
                    child = self._insert(mod_node, str(k) + (" (video)" if isinstance(obj, ImageSeries) else ""))
                    if is_timeseries_like:
                        self.data_nodes_map[id(child)] = (nwb_path, "processing", str(mod_name), str(k))

        # Stimulus
        stim = getattr(nwbfile, "stimulus", None)
        if stim:
            node = None
            for k, obj in stim.items():
                if node is None:
                    node = self._insert(None, "Stimulus", open_=True)
                child = self._insert(node, str(k) + (" (video)" if isinstance(obj, ImageSeries) else ""))
                if isinstance(obj, AnnotationSeries):
                    self.managed_object_nodes_map[id(child)] = {
                        "file_path": nwb_path,
                        "object_path": f"stimulus/{k}",
                        "neurodata_type": obj.__class__.__name__,
                        "plot_capable": False,
                    }
                else:
                    self.data_nodes_map[id(child)] = (nwb_path, "stimulus", str(k))

        # Intervals
        intervals = getattr(nwbfile, "intervals", None)
        if intervals:
            node = None
            for k, obj in intervals.items():
                if k == "trials":
                    continue
                if node is None:
                    node = self._insert(None, "Intervals", open_=True)
                child = self._insert(node, str(k))
                self.managed_object_nodes_map[id(child)] = {
                    "file_path": nwb_path,
                    "object_path": f"intervals/{k}",
                    "neurodata_type": obj.__class__.__name__,
                    "plot_capable": False,
                }

        # Dedicated event tables in newer NWB schema versions.
        event_tables = getattr(nwbfile, "events", None)
        if event_tables:
            events_root = self._insert(None, "Event tables", open_=False)
            try:
                event_items = list(event_tables.items())
            except Exception:
                event_items = []
            for event_name, event_table in event_items:
                event_item = self._insert(events_root, str(event_name))
                self.managed_object_nodes_map[id(event_item)] = {
                    "file_path": nwb_path,
                    "object_path": f"events/{event_name}",
                    "neurodata_type": event_table.__class__.__name__,
                    "plot_capable": False,
                }

        # Display complete sorting-project unit groups in addition to the
        # canonical table. These are the same groups offered by the Analysis
        # units selector.
        if getattr(nwbfile, "units", None) is not None:
            units_root = self._insert(None, "Units", open_=False)
            all_units_item = self._insert(units_root, "All units")
            self.managed_object_nodes_map[id(all_units_item)] = {
                "file_path": nwb_path,
                "object_path": "units",
                "neurodata_type": nwbfile.units.__class__.__name__,
                "plot_capable": False,
            }
            projects = []
            try:
                units_df = nwbfile.units.to_dataframe()
                if "sorting_project" in units_df.columns:
                    projects = sorted(
                        {
                            str(value).strip()
                            for value in units_df["sorting_project"].dropna().tolist()
                            if str(value).strip() and str(value).strip().lower() != "nan"
                        },
                        key=str.lower,
                    )
            except Exception:
                projects = []
            for sorting_project in projects:
                project_item = self._insert(units_root, sorting_project)
                self.managed_object_nodes_map[id(project_item)] = {
                    "file_path": nwb_path,
                    "object_path": f"units/{unit_project_path_segment(sorting_project)}",
                    "sorting_project": sorting_project,
                    "neurodata_type": nwbfile.units.__class__.__name__,
                    "plot_capable": False,
                }

        # Event markers
        self.events.build_catalog_for_file(nwb_path, nwbfile=nwbfile)
        event_options = self.events.list_options(nwb_path)
        if event_options:
            ev_node = self._insert(None, "Event markers", open_=False)
            for lab in event_options:
                child = self._insert(ev_node, lab)
                self.event_nodes_map[id(child)] = (nwb_path, lab)

    # ---------------- content clicks ----------------

    def _content_view_menu(self, position):
        item = self.content_panel.tree.itemAt(position)
        if item is None:
            return
        spec = self.data_nodes_map.get(id(item))
        if spec:
            nwb_path, object_path = spec[0], "/".join(spec[1:])
        else:
            info = self.folder_leaf_object_map.get(id(item), {})
            paths = info.get("file_paths", [])
            if len(paths) != 1:
                return
            nwb_path, object_path = paths[0], info.get("nwb_path", "")
        menu = QtWidgets.QMenu(self)
        add = menu.addAction("Add to current view")
        fresh = menu.addAction("Open in new shared view")
        overlay = None
        try:
            from GUI.panels.pose_video import read_pose_metadata
            if read_pose_metadata(nwb_path,object_path) is not None:
                overlay = menu.addAction("Show body part on associated video")
        except (OSError,KeyError,ValueError):
            pass
        chosen = menu.exec(self.content_panel.tree.viewport().mapToGlobal(position))
        if overlay is not None and chosen == overlay:
            if self._show_active_edit_for_path(nwb_path):return
            try:self.right_panel.show_pose_overlay(nwb_path,object_path)
            except Exception as exc:QtWidgets.QMessageBox.warning(self,"Video tracking",str(exc))
            return
        if chosen not in (add, fresh):
            return
        if self._show_active_edit_for_path(nwb_path):
            return
        try:
            if chosen == add and self.right_panel.shared_viewer is None:
                current = self.right_panel.stack.currentWidget()
                video = self.right_panel.video_panel
                context = self._current_plotted_object_context
                if video is not None and current is video and video.source:
                    source = video.source
                    self.right_panel.add_shared_object(source['nwb_path'], source['object_path'])
                elif current is self.right_panel.plot_page and context:
                    self.right_panel.add_shared_object(context['nwb_path'], context['object_path'])
            self.right_panel.add_shared_object(nwb_path, object_path, reset=chosen == fresh)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Shared viewer", str(exc))

    def on_content_item_click(self, item):
        key = id(item)
        if key in self.trials_nodes_map:
            self.on_trials_click(item)
            return
        if key in self.managed_object_nodes_map:
            object_info = self.managed_object_nodes_map[key]
            self._show_content_object_manager(
                object_path=object_info.get("object_path", ""),
                file_paths=[object_info.get("file_path", "")],
                neurodata_type=object_info.get("neurodata_type", ""),
                plot_capable=bool(object_info.get("plot_capable", False)),
                sorting_project=object_info.get("sorting_project", ""),
            )
            return
        if key in self.folder_leaf_files_map:
            object_info = self.folder_leaf_object_map.get(key, {})
            file_paths = list(
                object_info.get(
                    "file_paths",
                    self.folder_leaf_files_map.get(key, []),
                )
                or []
            )
            object_path = str(object_info.get("nwb_path", "") or "")
            neurodata_type = str(object_info.get("neurodata_type", "") or "")

            if neurodata_type in {"ImageSeries", "OpticalSeries", "OnePhotonSeries", "TwoPhotonSeries"} and file_paths:
                if any(self._show_active_edit_for_path(path) for path in file_paths):
                    return
                self.right_panel.show_video_view(file_paths, object_path)
                return

            # A content-summary row can still represent only one selected NWB
            # file. In that case, keep the normal single-file interaction:
            # display the data first and expose the object manager from the
            # plot controls. The manager remains the primary view only when
            # the row represents several files.
            parts = [part for part in object_path.strip("/").split("/") if part]
            event_types = {
                "AnnotationSeries",
                "BehavioralEpochs",
                "BehavioralEvents",
                "EventDetection",
                "EventsTable",
                "TimeIntervals",
                "Units",
            }
            plot_capable = (
                neurodata_type not in event_types
                and (
                    (len(parts) == 2 and parts[0] in {"acquisition", "stimulus"})
                    or (len(parts) == 3 and parts[0] == "processing")
                )
            )
            if len(file_paths) == 1 and plot_capable:
                self._plot_managed_object(
                    file_paths[0],
                    object_path,
                    neurodata_type=neurodata_type,
                )
                return

            self._show_content_object_manager(
                object_path=object_path,
                file_paths=file_paths,
                neurodata_type=neurodata_type,
                plot_capable=plot_capable,
                sorting_project=object_info.get("sorting_project", ""),
            )
            return
        if key in self.data_nodes_map:
            self.on_data_node_click(item)
            return
        if key in self.event_nodes_map:
            self.on_event_node_click(item)
            return

    def clear_trials_table(self):
        tt = self.right_panel.trials_table
        tt.clear()
        tt.setRowCount(0)
        tt.setColumnCount(0)

    def on_trials_click(self, item):
        nwb_path = self.trials_nodes_map.get(id(item))
        if not nwb_path:
            return
        if self._show_active_edit_for_path(nwb_path):
            return
        try:
            nwbfile = self.backend.open(nwb_path)
            if getattr(nwbfile, "trials", None) is None:
                return
            df_trials = nwbfile.trials.to_dataframe()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Trials", f"Could not read trials:\n{exc}")
            return
        trial_filters = self.filter_panel.get_trial_filters()
        if trial_filters is not None:
            identifier = None
            hit = self.df[self.df["file_path"].astype(str) == str(nwb_path)]
            if not hit.empty and "identifier" in hit.columns:
                identifier = str(hit.iloc[0]["identifier"])
            if identifier is None or identifier == "nan":
                identifier = Path(nwb_path).stem
            df_trials = filter_trial_df(trial_filters, df_trials, identifier)
        self.show_trials(df_trials)

    def show_trials(self, df_trials: pd.DataFrame):
        self.right_panel.show_trials_view()
        df = df_trials.copy()
        for col in df.select_dtypes(include="float").columns:
            df[col] = df[col].round(3)
        table = self.right_panel.trials_table
        table.clear()
        table.setRowCount(len(df))
        table.setColumnCount(len(df.columns))
        table.setHorizontalHeaderLabels([str(c) for c in df.columns])
        for r, (_, row) in enumerate(df.iterrows()):
            for c, col in enumerate(df.columns):
                item = QtWidgets.QTableWidgetItem(str(row[col]))
                table.setItem(r, c, item)
        table.resizeColumnsToContents()

    def closeEvent(self, event):
        dlc = getattr(self, 'deeplabcut_panel', None)
        if dlc is not None:
            if dlc.is_running():
                QtWidgets.QMessageBox.information(self, 'DeepLabCut',
                    'A DeepLabCut task is running. Wait for it to finish or cancel it before closing.')
                event.ignore()
                return
            dlc.shutdown()
        shared = getattr(getattr(self, "right_panel", None), "shared_viewer", None)
        if shared is not None:
            shared.shutdown()
        video = getattr(getattr(self, "right_panel", None), "video_panel", None)
        if video is not None:
            video.shutdown()
        super().closeEvent(event)

    def on_data_node_click(self, item):
        spec = self.data_nodes_map[id(item)]
        nwb_path = spec[0]
        if len(spec) == 4 and spec[1] == "processing":
            object_path = f"processing/{spec[2]}/{spec[3]}"
        elif len(spec) >= 3:
            object_path = f"{spec[1]}/{spec[2]}"
        else:
            return
        self._plot_managed_object(
            nwb_path,
            object_path,
            neurodata_type=item.text(0),
        )

    def _show_current_plotted_object_manager(self):
        context = self._current_plotted_object_context
        if not context:
            return
        self._show_content_object_manager(
            object_path=context["object_path"],
            file_paths=[context["nwb_path"]],
            neurodata_type=context.get("neurodata_type", ""),
            plot_capable=True,
        )

    def _show_content_object_manager(
        self,
        *,
        object_path: str,
        file_paths,
        neurodata_type: str = "",
        plot_capable: bool = False,
        sorting_project: str = "",
    ):
        manager = getattr(self, "content_object_manager", None)
        if manager is None:
            return
        if manager.is_busy():
            self._show_active_content_object_operation()
            return
        manager.set_object(
            object_path=str(object_path or ""),
            file_paths=list(file_paths or []),
            neurodata_type=str(neurodata_type or ""),
            plot_capable=bool(plot_capable),
            sorting_project=str(sorting_project or ""),
        )
        self.right_panel.show_content_manager_view()

    def _plot_managed_object(
        self,
        nwb_path: str,
        object_path: str,
        neurodata_type: str = "",
    ):
        if self._show_active_edit_for_path(nwb_path):
            return
        parts = [part for part in str(object_path or "").strip().strip("/").split("/") if part]
        if len(parts) == 3 and parts[0] == "processing":
            spec = (str(nwb_path), "processing", parts[1], parts[2])
        elif len(parts) == 2 and parts[0] in {"acquisition", "stimulus"}:
            spec = (str(nwb_path), parts[0], parts[1])
        else:
            QtWidgets.QMessageBox.information(
                self,
                "Plot NWB object",
                "This type of NWB object cannot be displayed in the time-series plot.",
            )
            return

        try:
            from GUI.panels.pose_video import read_pose_metadata
            if read_pose_metadata(nwb_path,object_path) is not None:
                self._current_plotted_object_context={"nwb_path":str(nwb_path),"object_path":str(object_path),"neurodata_type":neurodata_type}
                self.right_panel.add_shared_object(nwb_path,object_path,reset=True)
                return
            obj = self.backend.get_timeseries(str(nwb_path), ":".join(parts))
            if isinstance(obj, ImageSeries):
                self.right_panel.show_video_view([str(nwb_path)], str(object_path))
                return
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Display NWB object", f"Could not open object:\n{exc}")
            return

        if getattr(self.plot_panel, "current_ts_path", None) and hasattr(self.view_state, "update_from_indices"):
            ts_previous = self.plot_panel.get_current_ts()
            if ts_previous is not None:
                try:
                    self.view_state.update_from_indices(
                        ts_previous,
                        self.plot_panel.current_start_idx,
                        self.plot_panel.total_n,
                        self.plot_panel.view_n,
                    )
                except Exception:
                    pass
        try:
            self.plot_panel.show_data_plot(spec, str(nwb_path), self.filter_panel.get_trial_filters())
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Plot", f"Could not show plot:\n{exc}")
            return

        if not neurodata_type:
            manager = getattr(self, "content_object_manager", None)
            if manager is not None and str(getattr(manager, "object_path", "")) == str(object_path):
                neurodata_type = str(getattr(manager, "neurodata_type", ""))

        self._current_plotted_object_context = {
            "nwb_path": str(nwb_path),
            "object_path": str(object_path),
            "neurodata_type": str(neurodata_type or ""),
        }

    def prepare_nwb_object_edit(self, file_paths) -> bool:
        """Close read handles and retain UI state before a background NWB rewrite."""
        running = []
        for definition in self._workflow_tabs:
            if self._panel_is_running(definition["attribute"]):
                running.append(definition["title"])
        if running:
            QtWidgets.QMessageBox.warning(
                self,
                "NWB object manager",
                "Wait for the following operation(s) to finish before editing NWB files:\n"
                + "\n".join(running),
            )
            return False

        if getattr(self.backend, "is_locked", lambda: False)():
            QtWidgets.QMessageBox.warning(
                self,
                "NWB object manager",
                "An NWB file is currently locked by another operation. Wait for it to finish and try again.",
            )
            return False

        try:
            self._content_edit_tree_selection = list(self.file_tree_panel.selected_paths())
        except Exception:
            self._content_edit_tree_selection = []
        try:
            self._content_edit_file_filters = self.filter_panel.get_file_filters()
            self._content_edit_trial_filters = self.filter_panel.get_trial_filters()
        except Exception:
            self._content_edit_file_filters = {}
            self._content_edit_trial_filters = {}

        try:
            timer = getattr(self.plot_panel, "reload_timer", None)
            if timer is not None:
                timer.stop()
            self.plot_panel._invalidate_buffer_and_cache()
            edited_keys = {str(Path(path)) for path in file_paths}
            if str(getattr(self.plot_panel, "current_nwb_path", "")) in edited_keys:
                self.plot_panel.current_nwb_path = None
                self.plot_panel.current_ts_path = None
            self.backend.close()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self,
                "NWB object manager",
                f"Could not release the NWB file before editing:\n{exc}",
            )
            return False

        for path in file_paths:
            self.events.catalog_by_path.pop(str(path), None)
            self.events.active_by_path.pop(str(path), None)
        self._nwb_object_edit_active_paths = {
            self._content_edit_path_key(path) for path in file_paths
        }
        return True

    @staticmethod
    def _content_edit_path_key(path) -> str:
        return str(path or "").strip().replace("\\", "/").rstrip("/").lower()

    def _show_active_content_object_operation(self):
        manager = getattr(self, "content_object_manager", None)
        if manager is None or not manager.is_busy():
            return
        self.right_panel.show_content_manager_view()

    def _show_active_edit_for_path(self, nwb_path: str) -> bool:
        active_paths = set(getattr(self, "_nwb_object_edit_active_paths", set()) or set())
        if self._content_edit_path_key(nwb_path) not in active_paths:
            return False
        self._show_active_content_object_operation()
        self.statusBar().showMessage(
            "This NWB file is currently being modified. The active progress view was restored.",
            8000,
        )
        return True

    def _on_nwb_object_running_state_changed(self, running: bool, message: str):
        try:
            self.content_panel.set_running_state(bool(running), str(message or ""))
        except Exception:
            pass
        if not running:
            self._nwb_object_edit_active_paths = set()
        self._update_running_tab_colors()

    def _on_nwb_object_edit_finished(self, result: dict):
        """Reload the full inventory and rebuild the current Content tree."""
        selected_tree_paths = list(getattr(self, "_content_edit_tree_selection", []) or [])
        file_filters = dict(getattr(self, "_content_edit_file_filters", {}) or {})
        trial_filters = dict(getattr(self, "_content_edit_trial_filters", {}) or {})

        self.reload_inventory()
        self._df_master = self.df.copy(deep=True)
        self._content_df_master = self.content_df.copy(deep=True)
        try:
            self.filter_panel.load_filter_dicts(
                trial_filter_dict=trial_filters,
                file_filter_dict=file_filters,
                apply=True,
            )
        except Exception:
            pass

        tree = getattr(self.file_tree_panel, "tree", None)
        matching_items = []
        for path in selected_tree_paths:
            item = self.file_tree_panel.find_item_by_path(path)
            if item is not None:
                matching_items.append(item)
        if tree is not None and matching_items:
            signals_were_blocked = tree.blockSignals(True)
            try:
                tree.clearSelection()
                for item in matching_items:
                    item.setSelected(True)
                tree.setCurrentItem(matching_items[0])
            finally:
                tree.blockSignals(signals_were_blocked)
            self.on_select()

        action = str(result.get("action", "edit"))
        count = len(result.get("files", []) or [])
        self.statusBar().showMessage(f"NWB object {action} completed for {count} file(s)", 10000)

    def on_event_node_click(self, item):
        ref = self.event_nodes_map.get(id(item))
        if not ref:
            return
        nwb_path, label = ref
        if self._show_active_edit_for_path(nwb_path):
            return
        active = self.events.get_active(nwb_path)
        if label in active:
            active.remove(label)
            self.statusBar().showMessage(f"Removed event overlay: {label}")
        else:
            active.add(label)
            self.statusBar().showMessage(f"Added event overlay: {label}")
        try:
            self.plot_panel.refresh_plot()
        except Exception:
            pass


# DELIVERY_BUILD_ID: APP-20260812-01
