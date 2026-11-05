from __future__ import annotations

from typing import Optional, List

import numpy as np

from GUI.qt_compat import QtCore, QtGui, QtWidgets

try:
    import pyqtgraph as pg
except Exception as exc:  # allows app to open and show a clear message
    pg = None
    _PYQTGRAPH_IMPORT_ERROR = exc
else:
    _PYQTGRAPH_IMPORT_ERROR = None


def _qt_event_type(name):
    try:
        return getattr(QtCore.QEvent.Type, name)
    except AttributeError:
        return getattr(QtCore.QEvent, name)


def _native_scrollbar_without_arrow_buttons_stylesheet():
    """Keep the platform/native scrollbar look, but hide the end arrow buttons."""
    return """
QScrollBar::add-line:horizontal {
    width: 0px;
    border: none;
    background: transparent;
}
QScrollBar::sub-line:horizontal {
    width: 0px;
    border: none;
    background: transparent;
}
"""


class _PlotContextMenuFilter(QtCore.QObject):
    """Suppress the custom context menu when right-click was used as a drag."""

    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.right_press_pos = None
        self.right_dragged = False

    def eventFilter(self, watched, event):
        etype = event.type()

        if etype == _qt_event_type("MouseButtonPress"):
            try:
                is_right = event.button() == self.panel._right_button()
            except Exception:
                is_right = False
            if is_right:
                self.right_press_pos = event.pos()
                self.right_dragged = False
                self.panel._suppress_next_context_menu = False

        elif etype == _qt_event_type("MouseMove"):
            if self.right_press_pos is not None:
                try:
                    if event.buttons() & self.panel._right_button():
                        if (event.pos() - self.right_press_pos).manhattanLength() > 6:
                            self.right_dragged = True
                except Exception:
                    pass

        elif etype == _qt_event_type("MouseButtonRelease"):
            try:
                is_right = event.button() == self.panel._right_button()
            except Exception:
                is_right = False
            if is_right:
                if self.right_dragged:
                    self.panel._suppress_next_context_menu = True
                self.right_press_pos = None
                self.right_dragged = False

        elif etype == _qt_event_type("ContextMenu"):
            if bool(getattr(self.panel, "_suppress_next_context_menu", False)):
                self.panel._suppress_next_context_menu = False
                try:
                    event.accept()
                except Exception:
                    pass
                return True

        return False


class PlotPanel:
    """Native Qt/PyQtGraph replacement for the Tk/Matplotlib PlotPanel.

    The key behavior for the migration is that the plot is embedded normally in
    a Qt widget, and new trace chunks are loaded when the visible x-range moves
    outside the current buffer.
    """

    def __init__(
        self,
        *,
        right_panel,
        backend,
        events,
        view_state,
        probe_viewer=None,
        manage_object_callback=None,
    ):
        self.right_panel = right_panel
        self.backend = backend
        self.events = events
        self.view_state = view_state
        self.probe_viewer = probe_viewer
        self.manage_object_callback = manage_object_callback

        self.current_ts_path: Optional[str] = None
        self.current_name: Optional[str] = None
        self.current_nwb_path: Optional[str] = None
        self.total_n = 0
        self.view_n = 20_000
        self.current_start_idx = 0
        self.current_channel_index = 0
        self.current_channel_mode = "single"
        self.channel_names: List[str] = []

        self._timestamps_cache_key = None
        self._timestamps_cache = None
        self._buffer_start = 0
        self._buffer_end = 0
        self._buffer_x = None
        self._buffer_y = None
        self._max_buffer_n = 2_000_000
        # Load much more than the visible window so panning is smooth.
        # The viewer only reloads when the visible range approaches the edge
        # of this buffer instead of reloading at every mouse move.
        self._buffer_margin_windows = 4.0
        self._buffer_edge_fraction = 0.22
        self._setting_range = False
        self._pending_reload = False
        self._auto_y_next_refresh = True
        self._last_plotted_buffer_key = None

        self.current_ymin = None
        self.current_ymax = None
        self.y_limits_by_dataset = {}

        self.channel_combo = None
        self.pos_slider = None
        self.winsec_edit = None
        self.downsample_edit = None
        self._updating_window_spin = False
        self.ymin_edit = None
        self.ymax_edit = None
        self.events_combo = None
        self.event_alpha_spin = None
        self.event_color_edit = None
        self.info_label = None
        self.probe_dialog = None

        self.plot_widget = None
        self.plot_item = None
        self.data_item = None
        self.marker_item = None
        self.event_items = []
        self.grid_enabled = False
        self.trace_line_width = 1
        self.render_smoothing = True
        self._context_menu_filter = None
        self._suppress_next_context_menu = False

        self.reload_timer = QtCore.QTimer()
        self.reload_timer.setSingleShot(True)
        self.reload_timer.timeout.connect(self._reload_for_visible_range)

    # ---------------- dataset helpers ----------------

    def _get_ts(self):
        if not self.current_nwb_path or not self.current_ts_path:
            return None
        return self.backend.get_timeseries(self.current_nwb_path, self.current_ts_path, "r")

    def get_current_ts(self):
        return self._get_ts()

    def _invalidate_buffer_and_cache(self):
        self._timestamps_cache_key = None
        self._timestamps_cache = None
        self._buffer_start = 0
        self._buffer_end = 0
        self._buffer_x = None
        self._buffer_y = None

    def _ts_has_timestamps(self):
        ts = self._get_ts()
        return ts is not None and getattr(ts, "timestamps", None) is not None

    def _get_timestamps_array(self):
        ts = self._get_ts()
        if ts is None or getattr(ts, "timestamps", None) is None:
            return np.array([], dtype=float)
        cache_key = (self.current_nwb_path, self.current_ts_path)
        if self._timestamps_cache_key == cache_key and self._timestamps_cache is not None:
            return self._timestamps_cache
        try:
            arr = ts.timestamps[:]
        except Exception:
            arr = ts.timestamps
        arr = np.asarray(arr, dtype=float).reshape(-1)
        self._timestamps_cache_key = cache_key
        self._timestamps_cache = arr
        return arr

    def _window_seconds(self):
        try:
            return max(1e-6, float(getattr(self.view_state, "global_window_s", 5.0)))
        except Exception:
            return 5.0

    def _set_window_seconds(self, value_s):
        try:
            value = max(1e-6, float(value_s))
        except Exception:
            return
        try:
            self.view_state.global_window_s = value
        except Exception:
            pass

    def _set_window_spin_value(self, value_s):
        if self.winsec_edit is None:
            return
        try:
            max_value = max(0.005, self._timeline_duration_s() or 3600.0)
            self._updating_window_spin = True
            self.winsec_edit.blockSignals(True)
            self.winsec_edit.setMaximum(max_value)
            self.winsec_edit.setValue(max(0.005, min(max_value, float(value_s))))
        finally:
            try:
                self.winsec_edit.blockSignals(False)
            except Exception:
                pass
            self._updating_window_spin = False

    def _downsample(self):
        return 1

    def _time_to_index(self, t):
        ts = self._get_ts()
        if ts is None:
            return 0
        if getattr(ts, "timestamps", None) is not None:
            arr = self._get_timestamps_array()
            if arr.size == 0:
                return 0
            return int(np.searchsorted(arr, float(t), side="left"))
        rate = getattr(ts, "rate", None)
        if rate:
            return int(np.floor((float(t) - float(getattr(ts, "starting_time", 0.0))) * float(rate)))
        return int(t)

    def _index_to_time(self, idx):
        ts = self._get_ts()
        idx = int(np.clip(idx, 0, max(0, self.total_n - 1)))
        if ts is None:
            return float(idx)
        if getattr(ts, "timestamps", None) is not None:
            arr = self._get_timestamps_array()
            if arr.size:
                return float(arr[int(np.clip(idx, 0, arr.size - 1))])
        rate = getattr(ts, "rate", None)
        if rate:
            return float(getattr(ts, "starting_time", 0.0)) + float(idx) / float(rate)
        return float(idx)

    def _timeline_first_time(self):
        if self.total_n <= 0:
            return 0.0
        try:
            return float(self._index_to_time(0))
        except Exception:
            return 0.0

    def _timeline_last_time(self):
        if self.total_n <= 0:
            return 0.0
        try:
            return float(self._index_to_time(max(0, self.total_n - 1)))
        except Exception:
            return self._timeline_first_time()

    def _timeline_duration_s(self):
        first_t = self._timeline_first_time()
        last_t = self._timeline_last_time()
        if not np.isfinite(first_t) or not np.isfinite(last_t) or last_t <= first_t:
            return max(0.0, float(max(0, self.total_n - 1)))
        return max(0.0, float(last_t - first_t))

    def _update_scrollbar_range(self, start_time=None):
        if self.pos_slider is None:
            return
        first_t = self._timeline_first_time()
        duration_s = self._timeline_duration_s()
        win_s = max(0.001, min(max(0.001, duration_s if duration_s > 0 else self._window_seconds()), self._window_seconds()))
        duration_ms = max(1, int(round(duration_s * 1000.0)))
        window_ms = max(1, int(round(win_s * 1000.0)))
        max_start = max(0, duration_ms - window_ms)
        if start_time is None:
            try:
                x0, _ = self._current_visible_range()
                value = int(round((float(x0) - first_t) * 1000.0))
            except Exception:
                value = self.pos_slider.value()
        else:
            value = int(round((float(start_time) - first_t) * 1000.0))
        value = max(0, min(max_start, value))
        self.pos_slider.blockSignals(True)
        self.pos_slider.setMinimum(0)
        self.pos_slider.setMaximum(max_start)
        self.pos_slider.setPageStep(max(1, window_ms))
        self.pos_slider.setSingleStep(max(1, min(window_ms // 10, 1000)))
        self.pos_slider.setValue(value)
        self.pos_slider.blockSignals(False)

    def _set_scrollbar_to_time(self, start_time):
        self._update_scrollbar_range(start_time=start_time)

    def _view_n_from_window(self):
        ts = self._get_ts()
        if ts is None:
            return 1
        win_s = self._window_seconds()
        if getattr(ts, "timestamps", None) is not None:
            arr = self._get_timestamps_array()
            if arr.size < 2:
                return 1
            t0 = self._index_to_time(self.current_start_idx)
            e = int(np.searchsorted(arr, t0 + win_s, side="right"))
            return max(1, min(self.total_n, e - int(self.current_start_idx)))
        rate = getattr(ts, "rate", None)
        if rate:
            return max(1, min(self.total_n, int(round(win_s * float(rate)))))
        return max(1, min(self.total_n, int(round(win_s))))

    # ---------------- public entry ----------------

    def show_data_plot(self, spec, nwb_path: str, filter_dict=None):
        self.right_panel.show_plot_view()
        self.current_nwb_path = str(nwb_path)

        if isinstance(spec, str):
            self.current_ts_path = f"acquisition:{spec}"
            self.current_name = spec
        else:
            container = spec[1]
            if container == "acquisition":
                self.current_ts_path = f"acquisition:{spec[2]}"
                self.current_name = str(spec[2])
            elif container == "stimulus":
                self.current_ts_path = f"stimulus:{spec[2]}"
                self.current_name = str(spec[2])
            elif container == "processing":
                self.current_ts_path = f"processing:{spec[2]}:{spec[3]}"
                self.current_name = f"{spec[2]}/{spec[3]}"
            else:
                raise KeyError(f"Unknown container: {container}")

        ts = self._get_ts()
        if ts is None:
            raise RuntimeError("No TimeSeries selected.")
        self.total_n = int(self.backend.get_total_n(ts))
        self._invalidate_buffer_and_cache()
        self._auto_y_next_refresh = True
        self._last_plotted_buffer_key = None

        base_names = list(self.backend.get_channel_names(ts))
        if len(base_names) > 1:
            self.channel_names = ["Average"] + base_names
            self.current_channel_mode = "avg"
            self.current_channel_index = 0
        else:
            self.channel_names = base_names or ["data"]
            self.current_channel_mode = "single"
            self.current_channel_index = 0

        if self.backend.is_open(self.current_nwb_path):
            nwbfile = self.backend.nwbfile
        else:
            self.backend.close()
            nwbfile = self.backend.open(self.current_nwb_path)

        self._build_controls(nwbfile)
        self._build_plot_widget()

        # Restore global view position.
        self.view_n = self._view_n_from_window()
        if hasattr(self.view_state, "compute_start_idx"):
            self.current_start_idx = int(self.view_state.compute_start_idx(ts, self.total_n, self.view_n))
        else:
            self.current_start_idx = 0
        self._set_start_idx(self.current_start_idx, update_range=True)

    # ---------------- UI ----------------

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            sub = item.layout()
            if widget is not None:
                widget.deleteLater()
            elif sub is not None:
                self._clear_layout(sub)

    def _build_controls(self, nwbfile):
        self._clear_layout(self.right_panel.control_row1)
        self._clear_layout(self.right_panel.control_row2)
        self._clear_layout(self.right_panel.control_row3)

        row1 = self.right_panel.control_row1
        row1.addWidget(QtWidgets.QLabel("Channel:"))
        self.channel_combo = QtWidgets.QComboBox()
        self.channel_combo.addItems([str(x) for x in self.channel_names])
        self.channel_combo.currentIndexChanged.connect(self.on_channel_change)
        self.channel_combo.setMinimumWidth(140)
        self.channel_combo.setMaximumWidth(260)
        try:
            self.channel_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
        except Exception:
            try:
                self.channel_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
            except Exception:
                pass
        try:
            self.channel_combo.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
        except Exception:
            try:
                self.channel_combo.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
            except Exception:
                pass
        row1.addWidget(self.channel_combo, stretch=0)
        row1.addStretch(1)

        try:
            horizontal = QtCore.Qt.Orientation.Horizontal
        except AttributeError:
            horizontal = QtCore.Qt.Horizontal
        self.pos_slider = QtWidgets.QScrollBar(horizontal)
        self.pos_slider.setMinimum(0)
        self.pos_slider.setMaximum(0)
        self.pos_slider.setPageStep(1)
        self.pos_slider.setSingleStep(100)
        self.pos_slider.setStyleSheet(_native_scrollbar_without_arrow_buttons_stylesheet())
        self.pos_slider.sliderReleased.connect(self.on_slider_released)

        row2 = self.right_panel.control_row2
        self.events.build_catalog_for_file(self.current_nwb_path, nwbfile=nwbfile)
        row2.addWidget(QtWidgets.QLabel("Events:"))
        self.events_combo = QtWidgets.QComboBox()
        event_options = self.events.list_options(self.current_nwb_path)
        self.events_combo.addItems(event_options)
        self.events_combo.currentIndexChanged.connect(self.on_event_combo_change)
        row2.addWidget(self.events_combo)
        add_btn = QtWidgets.QPushButton("Add")
        rem_btn = QtWidgets.QPushButton("Remove")
        add_btn.clicked.connect(self.add_selected_event)
        rem_btn.clicked.connect(self.remove_selected_event)
        row2.addWidget(add_btn)
        row2.addWidget(rem_btn)
        row2.addWidget(QtWidgets.QLabel("Color:"))
        self.event_color_edit = QtWidgets.QLineEdit("C0")
        self.event_color_edit.setMaximumWidth(70)
        self.event_color_edit.editingFinished.connect(self.on_event_style_change)
        row2.addWidget(self.event_color_edit)
        row2.addWidget(QtWidgets.QLabel("Alpha:"))
        self.event_alpha_spin = QtWidgets.QDoubleSpinBox()
        self.event_alpha_spin.setRange(0.05, 1.0)
        self.event_alpha_spin.setDecimals(2)
        self.event_alpha_spin.setSingleStep(0.05)
        self.event_alpha_spin.setValue(0.30)
        self.event_alpha_spin.setMinimumWidth(95)
        self.event_alpha_spin.setMaximumWidth(110)
        self.event_alpha_spin.setMinimumHeight(28)
        self.event_alpha_spin.setKeyboardTracking(False)
        self.event_alpha_spin.valueChanged.connect(self.on_event_style_change)
        row2.addWidget(self.event_alpha_spin)
        row2.addStretch(1)

        row3 = self.right_panel.control_row3
        prev_evt = QtWidgets.QPushButton("Previous event")
        next_evt = QtWidgets.QPushButton("Next event")
        prev_evt.clicked.connect(self.goto_previous_event)
        next_evt.clicked.connect(self.goto_next_event)
        row3.addWidget(prev_evt)
        row3.addWidget(next_evt)
        probe_btn = QtWidgets.QPushButton("Probe")
        probe_btn.clicked.connect(self.on_probe_button)
        row3.addWidget(probe_btn)
        if self.manage_object_callback is not None:
            manage_btn = QtWidgets.QPushButton("Manage object")
            manage_btn.clicked.connect(
                lambda _checked=False: self.manage_object_callback()
            )
            row3.addWidget(manage_btn)
        self.info_label = QtWidgets.QLabel("")
        row3.addWidget(self.info_label, stretch=1)
        self._select_initial_event_option()
        self._sync_style_controls_from_selected()

    def _build_plot_widget(self):
        layout = self.right_panel.plot_frame_layout
        self._clear_layout(layout)
        if pg is None:
            msg = QtWidgets.QLabel(f"PyQtGraph could not be imported:\n{_PYQTGRAPH_IMPORT_ERROR}")
            msg.setWordWrap(True)
            layout.addWidget(msg)
            return
        try:
            # Antialiasing makes low-sampling-rate traces much less jagged.
            # Downsampling is still handled by PyQtGraph, but mean-mode below
            # avoids the rough vertical min/max look of peak downsampling.
            pg.setConfigOptions(antialias=bool(self.render_smoothing))
        except Exception:
            pass
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground("w")
        self.plot_item = self.plot_widget.getPlotItem()

        # Keep the viewer visually clean by default. The grid can be toggled
        # from the small custom right-click menu.
        self.grid_enabled = False
        self.plot_item.showGrid(x=False, y=False)
        self.plot_item.setLabel("bottom", "Time", units="s")
        self.plot_item.setTitle(str(self.current_name or ""))
        try:
            self.plot_item.getViewBox().setMouseMode(pg.ViewBox.PanMode)
        except Exception:
            pass

        # Disable PyQtGraph's large default context menu and replace it with
        # only the few actions that are useful in this GUI.
        try:
            self.plot_item.setMenuEnabled(False)
        except Exception:
            pass
        try:
            self.plot_item.getViewBox().setMenuEnabled(False)
        except Exception:
            pass
        try:
            self.plot_item.hideButtons()
        except Exception:
            pass
        try:
            custom_menu = QtCore.Qt.ContextMenuPolicy.CustomContextMenu
        except AttributeError:
            custom_menu = QtCore.Qt.CustomContextMenu
        self.plot_widget.setContextMenuPolicy(custom_menu)
        self.plot_widget.customContextMenuRequested.connect(self._show_plot_context_menu)

        # A right-click drag is used by PyQtGraph for navigation. Without this
        # filter Qt still emits a context-menu request when the mouse is
        # released, which makes the menu appear after every right-drag.
        self._suppress_next_context_menu = False
        self._context_menu_filter = _PlotContextMenuFilter(self)
        try:
            self.plot_widget.viewport().installEventFilter(self._context_menu_filter)
        except Exception:
            try:
                self.plot_widget.installEventFilter(self._context_menu_filter)
            except Exception:
                pass

        self.data_item = self.plot_item.plot([], [], pen=pg.mkPen("k", width=self.trace_line_width, cosmetic=True))
        # Keep a larger buffered trace in the item and let PyQtGraph render only
        # what is visible. This makes left-drag panning smooth and avoids
        # rebuilding the data item for every tiny range change.
        try:
            self.data_item.setClipToView(True)
        except Exception:
            pass
        try:
            self.data_item.setDownsampling(auto=False)
        except Exception:
            pass
        self.plot_item.sigRangeChanged.connect(self._on_range_changed)
        layout.addWidget(self.plot_widget, stretch=1)

        pos_row = QtWidgets.QHBoxLayout()
        pos_row.setSpacing(6)

        self.winsec_edit = QtWidgets.QDoubleSpinBox()
        self.winsec_edit.setDecimals(3)
        self.winsec_edit.setRange(0.005, max(0.005, self._timeline_duration_s() or 3600.0))
        self.winsec_edit.setSingleStep(0.5)
        self.winsec_edit.setSuffix(" s")
        self.winsec_edit.setMinimumWidth(95)
        self.winsec_edit.setMaximumWidth(125)
        self._set_window_spin_value(self._window_seconds())
        self.winsec_edit.valueChanged.connect(self.on_window_commit)

        pos_row.addWidget(QtWidgets.QLabel("Window:"))
        pos_row.addWidget(self.winsec_edit)
        pos_row.addWidget(QtWidgets.QLabel("Pos:"))
        pos_row.addWidget(self.pos_slider, stretch=1)
        layout.addLayout(pos_row)

    def _show_plot_context_menu(self, pos):
        if self.plot_widget is None or self.plot_item is None:
            return

        menu = QtWidgets.QMenu(self.plot_widget)

        grid_action = menu.addAction("Show grid")
        grid_action.setCheckable(True)
        grid_action.setChecked(bool(self.grid_enabled))
        grid_action.triggered.connect(lambda checked: self._set_grid_enabled(bool(checked)))

        save_action = menu.addAction("Save displayed plot...")
        save_action.triggered.connect(self._save_visible_plot)

        width_menu = menu.addMenu("Line width")
        for width in (1, 2, 3, 4, 5):
            act = width_menu.addAction(str(width))
            act.setCheckable(True)
            act.setChecked(int(self.trace_line_width) == int(width))
            act.triggered.connect(lambda checked=False, w=width: self._set_trace_line_width(w))

        try:
            global_pos = self.plot_widget.mapToGlobal(pos)
        except Exception:
            global_pos = QtGui.QCursor.pos()
        menu.exec(global_pos)

    def _set_grid_enabled(self, enabled: bool):
        self.grid_enabled = bool(enabled)
        if self.plot_item is not None:
            self.plot_item.showGrid(x=self.grid_enabled, y=self.grid_enabled, alpha=0.20)

    def _set_trace_line_width(self, width: int):
        self.trace_line_width = int(max(1, min(10, width)))
        if pg is not None and self.data_item is not None:
            self.data_item.setPen(pg.mkPen("k", width=self.trace_line_width, cosmetic=True))

    def _save_visible_plot(self):
        if self.plot_widget is None:
            return
        path, selected_filter = QtWidgets.QFileDialog.getSaveFileName(
            self.right_panel,
            "Save displayed plot",
            "displayed_plot.png",
            "PNG image (*.png);;PDF file (*.pdf);;JPEG image (*.jpg *.jpeg);;All files (*)",
        )
        if not path:
            return

        selected_filter = str(selected_filter or "")
        lower = path.lower()
        if selected_filter.startswith("PDF") and not lower.endswith(".pdf"):
            path += ".pdf"
            lower = path.lower()
        elif selected_filter.startswith("JPEG") and not (lower.endswith(".jpg") or lower.endswith(".jpeg")):
            path += ".jpg"
            lower = path.lower()
        elif selected_filter.startswith("PNG") and not lower.endswith(".png"):
            path += ".png"
            lower = path.lower()
        elif not (lower.endswith(".png") or lower.endswith(".pdf") or lower.endswith(".jpg") or lower.endswith(".jpeg")):
            path += ".png"
            lower = path.lower()

        try:
            if lower.endswith(".pdf"):
                ok = self._save_visible_plot_pdf(path)
            else:
                ok = self.plot_widget.grab().save(path)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self.right_panel, "Save plot", f"Could not save plot:\n{exc}")
            return
        if not ok:
            QtWidgets.QMessageBox.warning(self.right_panel, "Save plot", "Could not save plot.")

    def _save_visible_plot_pdf(self, path):
        try:
            writer = QtGui.QPdfWriter(path)
        except Exception:
            return False

        try:
            writer.setResolution(300)
        except Exception:
            pass

        try:
            page_size = QtGui.QPageSize(QtGui.QPageSize.PageSizeId.A4)
            writer.setPageSize(page_size)
        except Exception:
            try:
                writer.setPageSize(QtGui.QPagedPaintDevice.A4)
            except Exception:
                pass

        painter = QtGui.QPainter(writer)
        if not painter.isActive():
            return False
        try:
            self.plot_widget.render(painter)
        finally:
            painter.end()
        return True

    # ---------------- controls callbacks ----------------

    def on_channel_change(self, idx):
        self.current_channel_index = int(idx)
        if self.current_channel_index == 0 and len(self.channel_names) > 1 and self.channel_names[0] == "Average":
            self.current_channel_mode = "avg"
        else:
            self.current_channel_mode = "single"
        self._invalidate_buffer_and_cache()
        self._auto_y_next_refresh = True
        self._last_plotted_buffer_key = None
        self.refresh_plot()

    def on_slider_released(self):
        if self._setting_range or self.pos_slider is None:
            return
        first_t = self._timeline_first_time()
        start_time = first_t + float(self.pos_slider.value()) / 1000.0
        self._set_start_idx(self._time_to_index(start_time), update_range=True)

    def on_slider_move(self, value):
        # Kept only for compatibility with older code. The content browser now
        # uses a QScrollBar and moves on release, like the preprocessing browser.
        self.on_slider_released()

    def on_window_commit(self, value=None):
        if self._updating_window_spin:
            return
        if self.winsec_edit is not None:
            self._set_window_seconds(float(self.winsec_edit.value()))
        elif value is not None:
            self._set_window_seconds(float(value))
        self.view_n = self._view_n_from_window()
        self._auto_y_next_refresh = True
        self._set_start_idx(self.current_start_idx, update_range=True)

    def on_ylim_commit(self):
        try:
            ymin = self.ymin_edit.text().strip()
            ymax = self.ymax_edit.text().strip()
            if not ymin and not ymax:
                self.reset_ylim(); return
            ymin_f = float(ymin)
            ymax_f = float(ymax)
            if np.isfinite(ymin_f) and np.isfinite(ymax_f) and ymax_f > ymin_f:
                self.current_ymin = ymin_f
                self.current_ymax = ymax_f
                self.refresh_plot()
        except Exception:
            return

    def reset_ylim(self):
        self.current_ymin = None
        self.current_ymax = None
        if self.ymin_edit is not None:
            self.ymin_edit.setText("")
        if self.ymax_edit is not None:
            self.ymax_edit.setText("")
        self.refresh_plot()

    def step_position(self, direction: int):
        step = max(1, int(round(0.1 * max(1, self.view_n))))
        self._set_start_idx(self.current_start_idx + int(direction) * step, update_range=True)

    def _set_start_idx(self, start_idx: int, *, update_range: bool = False):
        maxpos = max(0, int(self.total_n - max(1, self.view_n)))
        self.current_start_idx = int(np.clip(start_idx, 0, maxpos))
        self._set_window_spin_value(self._window_seconds())
        if self.pos_slider is not None:
            self._setting_range = True
            try:
                self._set_scrollbar_to_time(self._index_to_time(self.current_start_idx))
            finally:
                self._setting_range = False
        if update_range and self.plot_item is not None:
            # Keep the displayed window in real seconds.  Do not derive x1
            # from view_n because switching between data types with different
            # sampling rates/timestamps can otherwise create a very zoomed-in
            # view.
            win_s = self._window_seconds()
            first_t = self._index_to_time(0)
            last_t = self._index_to_time(max(0, self.total_n - 1))
            x0 = self._index_to_time(self.current_start_idx)
            x1 = x0 + win_s
            if np.isfinite(last_t) and x1 > last_t and last_t > first_t:
                x1 = last_t
                x0 = max(first_t, x1 - win_s)
            if not np.isfinite(x0) or not np.isfinite(x1) or x1 <= x0:
                x0 = self._index_to_time(self.current_start_idx)
                x1 = x0 + win_s
            self._setting_range = True
            self.plot_item.setXRange(float(x0), float(x1), padding=0)
            self._setting_range = False
        self.refresh_plot()

    # ---------------- pyqtgraph interactions ----------------

    def _on_range_changed(self, *_):
        if self._setting_range:
            return
        try:
            x_min, x_max = self.plot_item.viewRange()[0]
        except Exception:
            return
        if not np.isfinite(x_min) or not np.isfinite(x_max) or x_max <= x_min:
            return

        width = float(x_max) - float(x_min)
        if np.isfinite(width) and width > 0:
            self._set_window_seconds(width)
            self._set_window_spin_value(width)

        start_idx = int(np.clip(self._time_to_index(x_min), 0, max(0, self.total_n - 1)))
        self.current_start_idx = start_idx
        if self.pos_slider is not None:
            self._setting_range = True
            try:
                self._set_scrollbar_to_time(float(x_min))
            finally:
                self._setting_range = False

        # Update the displayed slice from the full-resolution memory buffer.
        # A disk/NWB reload is only scheduled near the buffer edge.
        self._update_data_item_for_visible_range()
        if self._visible_needs_buffer_reload(float(x_min), float(x_max)):
            self.reload_timer.start(35)

    def _left_button(self):
        return QtCore.Qt.MouseButton.LeftButton if hasattr(QtCore.Qt, "MouseButton") else QtCore.Qt.LeftButton

    def _right_button(self):
        return QtCore.Qt.MouseButton.RightButton if hasattr(QtCore.Qt, "MouseButton") else QtCore.Qt.RightButton

    def _on_mouse_clicked(self, ev):
        # Left click is reserved for normal PyQtGraph pan/exploration.
        # The previous red vertical marker has intentionally been removed.
        return

    def _set_marker(self, x):
        if pg is None or self.plot_item is None:
            return
        if self.marker_item is None:
            self.marker_item = pg.InfiniteLine(pos=x, angle=90, movable=True, pen=pg.mkPen("r", width=2))
            self.plot_item.addItem(self.marker_item)
        else:
            self.marker_item.setValue(float(x))

    def _remove_marker(self):
        if self.marker_item is not None and self.plot_item is not None:
            self.plot_item.removeItem(self.marker_item)
            self.marker_item = None

    # ---------------- loading/plotting ----------------

    def _current_visible_range(self):
        if self.plot_item is None:
            x0 = self._index_to_time(self.current_start_idx)
            x1 = self._index_to_time(min(self.total_n - 1, self.current_start_idx + self.view_n))
            return x0, max(x0 + 1e-6, x1)
        try:
            x0, x1 = self.plot_item.viewRange()[0]
            if np.isfinite(x0) and np.isfinite(x1) and x1 > x0:
                return float(x0), float(x1)
        except Exception:
            pass
        x0 = self._index_to_time(self.current_start_idx)
        return x0, x0 + self._window_seconds()

    def _reload_for_visible_range(self):
        self.refresh_plot(force_reload=True)

    def _visible_needs_buffer_reload(self, x0=None, x1=None):
        if self._buffer_y is None or self._buffer_x is None:
            return True
        if x0 is None or x1 is None:
            x0, x1 = self._current_visible_range()
        if not np.isfinite(x0) or not np.isfinite(x1) or x1 <= x0:
            return False

        visible_start = int(np.clip(self._time_to_index(x0), 0, max(0, self.total_n - 1)))
        visible_stop = int(np.clip(self._time_to_index(x1), visible_start + 1, self.total_n))

        if visible_start < self._buffer_start or visible_stop > self._buffer_end:
            return True

        buffer_n = max(1, int(self._buffer_end - self._buffer_start))
        edge_n = max(1, int(round(buffer_n * float(self._buffer_edge_fraction))))
        return (visible_start - self._buffer_start) < edge_n or (self._buffer_end - visible_stop) < edge_n

    def _load_needed_buffer(self, *, force=False):
        ts = self._get_ts()
        if ts is None or self.total_n <= 0:
            return False

        x0, x1 = self._current_visible_range()
        width = max(1e-6, x1 - x0)

        if not force and not self._visible_needs_buffer_reload(x0, x1):
            return True

        margin = max(1.0, float(self._buffer_margin_windows))
        load_x0 = x0 - margin * width
        load_x1 = x1 + margin * width
        start = int(np.clip(self._time_to_index(load_x0), 0, max(0, self.total_n - 1)))
        stop = int(np.clip(self._time_to_index(load_x1), start + 1, self.total_n))

        # Limit buffer size; keep centered around visible range.
        if stop - start > self._max_buffer_n:
            center = int(np.clip(self._time_to_index((x0 + x1) * 0.5), 0, self.total_n - 1))
            half = self._max_buffer_n // 2
            start = int(np.clip(center - half, 0, max(0, self.total_n - 1)))
            stop = int(np.clip(start + self._max_buffer_n, start + 1, self.total_n))

        try:
            raw = ts.data[start:stop]
        except Exception as exc:
            if self.info_label is not None:
                self.info_label.setText(f"Could not load data: {exc}")
            return False

        data = np.asarray(raw)
        if data.ndim == 0:
            data = data.reshape(1)
        if data.ndim > 2:
            singleton_axes = tuple(
                axis for axis in range(2, data.ndim) if data.shape[axis] == 1
            )
            if singleton_axes:
                data = np.squeeze(data, axis=singleton_axes)
        if data.ndim > 2:
            raise NotImplementedError(
                f"Unsupported data shape {tuple(raw.shape)}. "
                "Only singleton dimensions after time and channels can be plotted automatically."
            )
        if data.ndim == 1:
            y = data.astype(float, copy=False)
        else:
            # assume time is axis 0. This matches NWB ElectricalSeries/TimeSeries.
            if self.current_channel_mode == "avg":
                y = np.nanmean(data.astype(float, copy=False), axis=1)
            else:
                ch = self.current_channel_index
                if self.channel_names and self.channel_names[0] == "Average":
                    ch -= 1
                ch = int(np.clip(ch, 0, data.shape[1] - 1))
                y = data[:, ch].astype(float, copy=False)

        if getattr(ts, "timestamps", None) is not None:
            arr = self._get_timestamps_array()
            x = arr[start:stop]
        else:
            rate = getattr(ts, "rate", None)
            if rate:
                t0 = float(getattr(ts, "starting_time", 0.0))
                x = t0 + np.arange(start, stop, dtype=float) / float(rate)
            else:
                x = np.arange(start, stop, dtype=float)

        self._buffer_start = start
        self._buffer_end = stop
        self._buffer_x = np.asarray(x, dtype=float)
        self._buffer_y = np.asarray(y, dtype=float)
        self._last_plotted_buffer_key = None
        if self.info_label is not None:
            self.info_label.setText(f"Loaded samples {start:,}–{stop:,}")
        return True

    def _apply_y_range_once_from_visible_data(self):
        if self.plot_item is None or self._buffer_x is None or self._buffer_y is None:
            return

        # Use the full loaded buffer, not only the currently visible slice.
        # This avoids over-zooming when switching between signals with different
        # units/sampling rates, and it keeps the Y scale stable during buffered
        # panning.  Percentiles ignore rare spikes/outliers, then we add a
        # generous margin so slow/low-amplitude signals do not fill the whole
        # height aggressively.
        vals = np.asarray(self._buffer_y, dtype=float)
        vals = vals[np.isfinite(vals)]
        if vals.size == 0:
            return

        if vals.size >= 20:
            ymin = float(np.nanpercentile(vals, 1.0))
            ymax = float(np.nanpercentile(vals, 99.0))
        else:
            ymin = float(np.nanmin(vals))
            ymax = float(np.nanmax(vals))

        if not np.isfinite(ymin) or not np.isfinite(ymax):
            return

        if ymax <= ymin:
            center = 0.5 * (ymin + ymax)
            scale = max(1.0, abs(center) * 0.20)
            ymin = center - scale
            ymax = center + scale
        else:
            span = ymax - ymin
            pad = max(span * 0.35, 1e-12)
            ymin -= pad
            ymax += pad

        self._setting_range = True
        try:
            self.plot_item.setYRange(ymin, ymax, padding=0)
        finally:
            self._setting_range = False

    def _set_data_item_downsampling_for_slice(self, n_points: int):
        if self.data_item is None:
            return
        try:
            if int(n_points) <= 350_000:
                self.data_item.setDownsampling(auto=False)
            else:
                self.data_item.setDownsampling(auto=True, method="peak")
            self.data_item.setClipToView(True)
        except Exception:
            pass

    def _update_data_item_for_visible_range(self):
        """Draw only the visible part of the full-resolution loaded buffer."""
        if self.data_item is None or self._buffer_x is None or self._buffer_y is None:
            return
        x = np.asarray(self._buffer_x, dtype=float)
        y = np.asarray(self._buffer_y, dtype=float)
        if x.size == 0 or y.size == 0:
            self.data_item.setData([], [])
            return
        try:
            x0, x1 = self._current_visible_range()
        except Exception:
            x0, x1 = float(x[0]), float(x[-1])
        if not np.isfinite(x0) or not np.isfinite(x1) or x1 <= x0:
            x0, x1 = float(x[0]), float(x[-1])
        width = max(1e-9, float(x1) - float(x0))
        pad = width * 0.15
        lo = max(float(x[0]), float(x0) - pad)
        hi = min(float(x[-1]), float(x1) + pad)
        i0 = int(np.searchsorted(x, lo, side="left"))
        i1 = int(np.searchsorted(x, hi, side="right"))
        i0 = max(0, min(i0, len(x) - 1))
        i1 = max(i0 + 1, min(i1, len(x)))
        xs = x[i0:i1]
        ys = y[i0:i1]
        self.data_item.setData(xs, ys)
        self._set_data_item_downsampling_for_slice(len(xs))

    def refresh_plot(self, *, force_reload=False):
        if self.data_item is None or self.plot_item is None:
            return
        if not self._load_needed_buffer(force=force_reload):
            return

        x = self._buffer_x
        y = self._buffer_y
        if x is None or y is None or x.size == 0:
            self.data_item.setData([], [])
            return

        # Keep the current visible range in real seconds before setting the
        # buffered data. This avoids PyQtGraph choosing an over-coarse adaptive
        # downsampling level from the full buffer on the first draw.
        try:
            x0, x1 = self._current_visible_range()
            self._setting_range = True
            self.plot_item.setXRange(float(x0), float(x1), padding=0)
        finally:
            self._setting_range = False

        # Keep the loaded buffer full-resolution, but only send the visible
        # part plus a small margin to PyQtGraph.  This avoids zooming into a
        # downsampled-looking first draw while preserving smooth panning.
        self._update_data_item_for_visible_range()

        if self._auto_y_next_refresh:
            self._apply_y_range_once_from_visible_data()
            self._auto_y_next_refresh = False

        event_x0 = float(x[0])
        event_x1 = float(x[-1])
        self._draw_active_events(event_x0, event_x1)

    # ---------------- events ----------------

    def _active_event_label(self):
        if self.events_combo is None or self.events_combo.currentText() == "":
            return None
        return str(self.events_combo.currentText())

    def on_event_combo_change(self, *_):
        self._sync_style_controls_from_selected()

    def _select_initial_event_option(self):
        if self.events_combo is None or not self.current_nwb_path:
            return
        options = [self.events_combo.itemText(i) for i in range(self.events_combo.count())]
        if not options:
            return
        active_here = [e for e in options if e in self.events.get_active(self.current_nwb_path)]
        if active_here:
            idx = self.events_combo.findText(active_here[0])
            if idx >= 0:
                self.events_combo.setCurrentIndex(idx)

    def _sync_style_controls_from_selected(self):
        lab = self._active_event_label()
        if not lab:
            return
        style = self.events.get_style(lab)
        if self.event_color_edit is not None:
            col = style.get("color")
            if col is not None:
                self.event_color_edit.setText(str(col))
        if self.event_alpha_spin is not None:
            try:
                self.event_alpha_spin.blockSignals(True)
                self.event_alpha_spin.setValue(float(style.get("alpha", 0.30)))
            finally:
                self.event_alpha_spin.blockSignals(False)

    def on_event_style_change(self, *_):
        lab = self._active_event_label()
        if not lab:
            return
        style = self.events.get_style(lab)
        if self.event_color_edit is not None:
            style["color"] = self.event_color_edit.text().strip() or None
        if self.event_alpha_spin is not None:
            style["alpha"] = float(self.event_alpha_spin.value())
        self.refresh_plot()

    def add_selected_event(self):
        lab = self._active_event_label()
        if not lab or not self.current_nwb_path:
            return
        self.events.get_active(self.current_nwb_path).add(lab)
        self.on_event_style_change()

    def remove_selected_event(self):
        lab = self._active_event_label()
        if not lab or not self.current_nwb_path:
            return
        self.events.get_active(self.current_nwb_path).discard(lab)
        self.refresh_plot()

    def _clear_event_items(self):
        if self.plot_item is None:
            return
        for item in self.event_items:
            try:
                self.plot_item.removeItem(item)
            except Exception:
                pass
        self.event_items = []

    def _qt_color(self, value=None, *, alpha: float = 1.0):
        """Return a QColor from common Matplotlib/Tk/PyQtGraph color strings."""
        text = "" if value is None else str(value).strip()
        if not text:
            text = "C0"

        mpl_cycle = {
            "C0": "#1f77b4",
            "C1": "#ff7f0e",
            "C2": "#2ca02c",
            "C3": "#d62728",
            "C4": "#9467bd",
            "C5": "#8c564b",
            "C6": "#e377c2",
            "C7": "#7f7f7f",
            "C8": "#bcbd22",
            "C9": "#17becf",
        }
        short = {
            "b": "blue",
            "g": "green",
            "r": "red",
            "c": "cyan",
            "m": "magenta",
            "y": "yellow",
            "k": "black",
            "w": "white",
        }
        text = mpl_cycle.get(text, mpl_cycle.get(text.upper(), short.get(text.lower(), text)))
        color = QtGui.QColor(text)
        if not color.isValid():
            color = QtGui.QColor("#1f77b4")
        color.setAlphaF(float(np.clip(alpha, 0.0, 1.0)))
        return color

    def _draw_active_events(self, x0, x1):
        if pg is None or self.plot_item is None or not self.current_nwb_path:
            return
        self._clear_event_items()
        try:
            nwbfile = self.backend.open(self.current_nwb_path)
        except Exception:
            return
        active = self.events.get_active(self.current_nwb_path)
        if not active:
            return

        for label in sorted(active):
            style = self.events.get_style(label)
            color_text = style.get("color") or "C0"
            alpha = float(style.get("alpha", 0.30))
            qcolor = self._qt_color(color_text, alpha=alpha)
            pen = pg.mkPen(qcolor, width=1)
            brush = pg.mkBrush(qcolor)

            try:
                points = self.events.get_points(nwbfile, self.current_nwb_path, label)
            except Exception:
                points = np.array([], dtype=float)
            points = np.asarray(points, dtype=float)
            points = points[np.isfinite(points)]
            for t in points:
                if x0 <= t <= x1:
                    line = pg.InfiniteLine(pos=float(t), angle=90, movable=False, pen=pen)
                    self.plot_item.addItem(line)
                    self.event_items.append(line)

            try:
                spans = self.events.get_spans(nwbfile, self.current_nwb_path, label)
            except Exception:
                spans = np.empty((0, 2), dtype=float)
            spans = np.asarray(spans, dtype=float)
            if spans.size:
                spans = spans.reshape(-1, 2)
            else:
                spans = np.empty((0, 2), dtype=float)
            for s, e in spans:
                if np.isfinite(s) and np.isfinite(e) and e >= x0 and s <= x1:
                    reg = pg.LinearRegionItem(
                        values=(float(max(s, x0)), float(min(e, x1))),
                        movable=False,
                        brush=brush,
                        pen=pen,
                    )
                    self.plot_item.addItem(reg)
                    self.event_items.append(reg)

    def _get_sorted_event_centers(self, label: str) -> np.ndarray:
        if not label or not self.current_nwb_path:
            return np.array([], dtype=float)
        try:
            nwbfile = self.backend.open(self.current_nwb_path)
            centers = self.events.get_event_centers(nwbfile, self.current_nwb_path, label)
        except Exception:
            centers = np.array([], dtype=float)
        centers = np.asarray(centers, dtype=float)
        centers = centers[np.isfinite(centers)]
        centers.sort()
        return centers

    @staticmethod
    def _nearest_event_index(centers: np.ndarray, t_now: float) -> int:
        if centers.size == 0:
            return -1
        j = int(np.searchsorted(centers, t_now, side="left"))
        if j <= 0:
            return 0
        if j >= centers.size:
            return int(centers.size - 1)
        return int(j if abs(centers[j] - t_now) < abs(centers[j - 1] - t_now) else (j - 1))

    def goto_next_event(self):
        lab = self._active_event_label()
        if not lab:
            return
        centers = self._get_sorted_event_centers(lab)
        if centers.size == 0:
            if self.info_label is not None:
                self.info_label.setText(f"No event center found for {lab}")
            return
        x0, x1 = self._current_visible_range()
        t_now = 0.5 * (x0 + x1)
        i = self._nearest_event_index(centers, float(t_now))
        if i < 0:
            return
        i_next = min(i + 1, int(centers.size - 1))
        self._center_on_time(float(centers[i_next]))

    def goto_previous_event(self):
        lab = self._active_event_label()
        if not lab:
            return
        centers = self._get_sorted_event_centers(lab)
        if centers.size == 0:
            if self.info_label is not None:
                self.info_label.setText(f"No event center found for {lab}")
            return
        x0, x1 = self._current_visible_range()
        t_now = 0.5 * (x0 + x1)
        i = self._nearest_event_index(centers, float(t_now))
        if i < 0:
            return
        i_prev = max(i - 1, 0)
        self._center_on_time(float(centers[i_prev]))

    def _center_on_time(self, t):
        half = 0.5 * self._window_seconds()
        start_idx = self._time_to_index(float(t) - half)
        self._set_start_idx(start_idx, update_range=True)

    # ---------------- probe viewer ----------------

    @staticmethod
    def _electrode_labels(table, rows: np.ndarray):
        for col in ("contact_ids", "contact_id", "label", "name", "id"):
            try:
                if col in table.colnames:
                    vals = table[col].data[rows]
                    return [str(v) for v in vals]
            except Exception:
                pass
        return [str(int(r)) for r in rows]

    def _probe_geometry_from_timeseries(self, ts):
        if ts is None or not hasattr(ts, "electrodes") or ts.electrodes is None:
            return None, None
        try:
            reg = ts.electrodes
            table = reg.table
            rows = np.asarray(reg.data[:], dtype=int)
        except Exception:
            return None, None

        try:
            colnames = list(table.colnames)
        except Exception:
            colnames = []

        try:
            if "x" in colnames and "y" in colnames:
                x = np.asarray(table["x"].data[rows], dtype=float)
                y = np.asarray(table["y"].data[rows], dtype=float)
            elif "rel_x" in colnames and "rel_y" in colnames:
                x = np.asarray(table["rel_x"].data[rows], dtype=float)
                y = np.asarray(table["rel_y"].data[rows], dtype=float)
            else:
                return None, None
        except Exception:
            return None, None

        labels = self._electrode_labels(table, rows)
        pos = np.c_[x, y]
        ok = np.isfinite(pos).all(axis=1)
        if not np.any(ok):
            return None, None
        return pos[ok], [lab for lab, keep in zip(labels, ok) if keep]

    def _selected_probe_index_and_label(self):
        selected_index = None
        selected_label = None
        if self.current_channel_mode == "single" and self.channel_names:
            if self.channel_names[0] == "Average":
                if self.current_channel_index > 0:
                    selected_index = max(0, self.current_channel_index - 1)
                    selected_label = self.channel_names[self.current_channel_index]
            else:
                selected_index = int(self.current_channel_index)
                selected_label = self.channel_names[self.current_channel_index]
        return selected_index, selected_label

    def on_probe_button(self):
        if pg is None:
            QtWidgets.QMessageBox.warning(self.right_panel, "Probe", "PyQtGraph is not available.")
            return
        ts = self._get_ts()
        pos, labels = self._probe_geometry_from_timeseries(ts)
        if pos is None or labels is None:
            QtWidgets.QMessageBox.information(
                self.right_panel,
                "Probe",
                "No electrodes geometry available for this dataset.\nExpected electrodes table columns like x/y or rel_x/rel_y.",
            )
            return

        selected_index, selected_label = self._selected_probe_index_and_label()

        dlg = QtWidgets.QDialog(self.right_panel)
        dlg.setWindowTitle("Probe viewer")
        dlg.resize(760, 820)
        layout = QtWidgets.QVBoxLayout(dlg)
        plot = pg.PlotWidget(dlg)
        plot.setBackground("w")
        plot.setAspectLocked(True)
        plot.showGrid(x=True, y=True, alpha=0.25)
        plot.setLabel("bottom", "x")
        plot.setLabel("left", "y")
        layout.addWidget(plot)

        # Make the probe clearly visible: larger contacts, stronger outline,
        # and a calm blue fill instead of tiny grey points.
        scatter = pg.ScatterPlotItem(
            x=pos[:, 0],
            y=pos[:, 1],
            size=20,
            pen=pg.mkPen(QtGui.QColor("#1f2937"), width=1.8),
            brush=pg.mkBrush(QtGui.QColor("#9ecae1")),
        )
        plot.addItem(scatter)

        if selected_index is not None and 0 <= int(selected_index) < len(pos):
            i = int(selected_index)
            hi = pg.ScatterPlotItem(
                x=[float(pos[i, 0])],
                y=[float(pos[i, 1])],
                size=36,
                pen=pg.mkPen(QtGui.QColor("#d62728"), width=3.2),
                brush=pg.mkBrush(QtGui.QColor(255, 255, 255, 0)),
            )
            plot.addItem(hi)
            if selected_label:
                plot.setTitle(f"Selected: {selected_label}")
            else:
                plot.setTitle("Selected channel")
        else:
            plot.setTitle("Probe")

        label_font = QtGui.QFont()
        label_font.setPointSize(9)
        label_font.setBold(True)
        for (x, y), lab in zip(pos, labels):
            txt = pg.TextItem(str(lab), anchor=(0.5, 0.5), color="#111827")
            try:
                txt.setFont(label_font)
            except Exception:
                pass
            txt.setPos(float(x), float(y))
            plot.addItem(txt)

        try:
            x_pad = max(20.0, float(np.nanptp(pos[:, 0])) * 0.10)
            y_pad = max(20.0, float(np.nanptp(pos[:, 1])) * 0.10)
            plot.setXRange(float(np.nanmin(pos[:, 0]) - x_pad), float(np.nanmax(pos[:, 0]) + x_pad), padding=0)
            plot.setYRange(float(np.nanmin(pos[:, 1]) - y_pad), float(np.nanmax(pos[:, 1]) + y_pad), padding=0)
        except Exception:
            pass

        self.probe_dialog = dlg
        dlg.show()
