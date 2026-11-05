"""Read-only external ImageSeries player. Decoding stays off the Qt GUI thread."""
from __future__ import annotations

from pathlib import Path
import threading
import time

import h5py
import numpy as np
from GUI.qt_compat import QtCore, QtGui, QtWidgets, qt_horizontal, qt_align_center


def _text(value):
    return value.decode('utf-8') if isinstance(value, bytes) else str(value)


def read_video_metadata(nwb_path, object_path):
    """Detach metadata from HDF5; never retain an NWB handle during playback."""
    with h5py.File(str(nwb_path), 'r') as file:
        group = file[str(object_path).strip('/')]
        if 'external_file' not in group:
            raise ValueError('This ImageSeries contains embedded images. This player currently supports external videos.')
        dataset = group['external_file']
        names = [_text(v) for v in np.asarray(dataset).reshape(-1)]
        if not names:
            raise ValueError('This ImageSeries has no external video paths.')
        starts = np.asarray(dataset.attrs.get('starting_frame', [0]), dtype=np.int64).reshape(-1)
        if len(starts) != len(names) or starts[0] != 0 or np.any(np.diff(starts) <= 0):
            raise ValueError('Invalid starting_frame metadata for the external video files.')
        if 'timestamps' in group:
            timestamps = np.asarray(group['timestamps'], dtype=float)
        elif 'starting_time' in group and 'num_samples' in group:
            rate = float(group['starting_time'].attrs.get('rate', 0))
            count = int(np.asarray(group['num_samples']).item())
            if not np.isfinite(rate) or rate <= 0 or count <= 0:
                raise ValueError('Invalid video rate or sample count.')
            timestamps = float(group['starting_time'][()])+np.arange(count)/rate
        else:
            raise ValueError('Video needs per-frame timestamps or starting_time, rate and num_samples.')
        if (timestamps.ndim != 1 or not len(timestamps) or not np.all(np.isfinite(timestamps))
                or np.any(np.diff(timestamps) <= 0) or starts[-1] >= len(timestamps)):
            raise ValueError('Video frame timestamps must be finite and strictly increasing.')
    from GUI.project_paths import resolve_video_path
    paths = [Path(resolve_video_path(name, nwb_path)) for name in names]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError('External video not found:\n'+'\n'.join(missing)+
                                '\nKeep the NWB and video in their original relative folder layout.')
    return dict(paths=paths, starts=starts, timestamps=timestamps, nwb_path=str(nwb_path), object_path=object_path)


class VideoDecoder(QtCore.QThread):
    frame_ready = QtCore.Signal(int, object)
    failed = QtCore.Signal(str)

    def __init__(self, source, parent=None):
        super().__init__(parent)
        self.source = source
        self.condition = threading.Condition()
        self.target = None
        self.stopping = False

    def request(self, index):
        with self.condition:
            self.target = int(index)
            self.condition.notify()

    def stop(self):
        with self.condition:
            self.stopping = True
            self.condition.notify()

    def run(self):
        container = None
        segment, local_index, iterator = -1, -1, None
        try:
            import av
            while True:
                with self.condition:
                    while self.target is None and not self.stopping:
                        self.condition.wait()
                    if self.stopping:
                        return
                    target, self.target = self.target, None
                part = int(np.searchsorted(self.source['starts'], target, side='right')-1)
                local_target = target-int(self.source['starts'][part])
                if part != segment or local_target <= local_index:
                    if container is not None:
                        container.close()
                    container = av.open(str(self.source['paths'][part]))
                    iterator = iter(container.decode(video=0))
                    segment, local_index = part, -1
                # Decode in order to preserve exact frame identity, including VFR.
                # A new request supersedes a long seek without blocking the UI.
                superseded = False
                while local_index < local_target:
                    with self.condition:
                        if self.stopping:
                            return
                        if self.target is not None:
                            superseded = True
                            break
                    try:
                        frame = next(iterator)
                    except StopIteration:
                        raise ValueError('Video ends before the frame listed in NWB. Check video/timestamp correspondence.')
                    local_index += 1
                if not superseded:
                    self.frame_ready.emit(target, frame.to_ndarray(format='rgb24'))
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if container is not None:
                container.close()


class ZoomableVideoImage(QtWidgets.QLabel):
    """Fit-relative zoom applied to the complete frame, including tracking overlays."""
    zoomChanged = QtCore.Signal(float)

    def __init__(self, text='', parent=None):
        super().__init__(text, parent)
        self.frame_pixmap = None
        self.zoom = 1.0
        self.pan = QtCore.QPointF()
        self.drag_start = None
        self.setToolTip('Mouse wheel: zoom at pointer. Left-drag: pan. Double-click: fit video.')

    def setPixmap(self, pixmap):
        self.frame_pixmap = pixmap
        self.update()

    def clear(self):
        self.frame_pixmap = None
        self.reset_view()
        super().clear()

    def reset_view(self):
        self.zoom = 1.0
        self.pan = QtCore.QPointF()
        self.zoomChanged.emit(self.zoom)
        self.update()

    def zoom_by(self, factor, anchor=None):
        if self.frame_pixmap is None:
            return
        old = self.zoom
        self.zoom = min(20.0, max(1.0, self.zoom * factor))
        if self.zoom == 1.0:
            self.pan = QtCore.QPointF()
        else:
            center = QtCore.QPointF(self.width()/2, self.height()/2)
            anchor = center if anchor is None else anchor
            ratio = self.zoom / old
            self.pan = anchor - center - (anchor - center - self.pan) * ratio
        self.zoomChanged.emit(self.zoom)
        self.update()

    def paintEvent(self, event):
        if self.frame_pixmap is None or self.frame_pixmap.isNull():
            super().paintEvent(event)
            return
        pixmap = self.frame_pixmap
        scale = min(self.width()/pixmap.width(), self.height()/pixmap.height()) * self.zoom
        width, height = pixmap.width()*scale, pixmap.height()*scale
        target = QtCore.QRectF((self.width()-width)/2+self.pan.x(),
                              (self.height()-height)/2+self.pan.y(), width, height)
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor('#16191d'))
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(target, pixmap, QtCore.QRectF(pixmap.rect()))
        painter.end()

    @staticmethod
    def event_position(event):
        return event.position() if hasattr(event, 'position') else QtCore.QPointF(event.pos())

    def wheelEvent(self, event):
        self.zoom_by(1.2 ** (event.angleDelta().y()/120), self.event_position(event))
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self.zoom > 1:
            self.drag_start = self.event_position(event)
            self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.drag_start is not None:
            position = self.event_position(event)
            self.pan += position-self.drag_start
            self.drag_start = position
            self.update()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.drag_start = None
        self.unsetCursor()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.reset_view()
        event.accept()


class VideoPanel(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.decoder = None
        self.source = None
        self.current = 0
        self._pixmap = None
        self._playing = False
        self._playback_rate = 1.0
        self.event_data = {}
        self.displayed = 0
        layout = QtWidgets.QVBoxLayout(self)
        self.title = QtWidgets.QLabel('NWB video')
        self.title.setWordWrap(True)
        layout.addWidget(self.title)
        self.file_choice = QtWidgets.QComboBox()
        self.file_choice.currentIndexChanged.connect(self._file_changed)
        layout.addWidget(self.file_choice)
        self.image = ZoomableVideoImage('Select an external ImageSeries in Content.')
        self.image.setAlignment(qt_align_center())
        self.image.setMinimumSize(160, 120)
        self.image.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Expanding)
        self.image.setStyleSheet('background: #16191d; color: white;')
        zoom_row = QtWidgets.QHBoxLayout()
        zoom_row.addStretch(1)
        self.zoom_out = QtWidgets.QPushButton('−')
        self.zoom_in = QtWidgets.QPushButton('+')
        self.zoom_fit = QtWidgets.QPushButton('Fit')
        self.zoom_level = QtWidgets.QLabel('100%')
        self.zoom_out.setToolTip('Zoom out')
        self.zoom_in.setToolTip('Zoom in')
        self.zoom_out.clicked.connect(lambda: self.image.zoom_by(1/1.2))
        self.zoom_in.clicked.connect(lambda: self.image.zoom_by(1.2))
        self.zoom_fit.clicked.connect(self.image.reset_view)
        self.image.zoomChanged.connect(lambda value: self.zoom_level.setText(f'{value*100:.0f}%'))
        for widget in (self.zoom_out, self.zoom_in, self.zoom_fit, self.zoom_level):
            zoom_row.addWidget(widget)
        layout.addLayout(zoom_row)
        layout.addWidget(self.image, 1)
        self.slider = QtWidgets.QSlider(qt_horizontal())
        self.slider.setTracking(False)
        self.slider.valueChanged.connect(self.seek)
        layout.addWidget(self.slider)
        row = QtWidgets.QHBoxLayout()
        self.previous = QtWidgets.QPushButton('Previous frame')
        self.play = QtWidgets.QPushButton('Play')
        self.next = QtWidgets.QPushButton('Next frame')
        self.previous.clicked.connect(lambda: self.step(-1))
        self.next.clicked.connect(lambda: self.step(1))
        self.play.clicked.connect(self.toggle_play)
        for button in [self.previous, self.next, self.play]:
            row.addWidget(button)
        row.addWidget(QtWidgets.QLabel('Speed'))
        self.playback_speed = QtWidgets.QComboBox()
        for rate in (.1, .25, .5, .75, 1., 1.5, 2., 4.):
            self.playback_speed.addItem(f'{rate:g}×', rate)
        self.playback_speed.setCurrentIndex(self.playback_speed.findData(1.0))
        self.playback_speed.setToolTip('Playback speed only. NWB timestamps and frame stepping remain unchanged.')
        self.playback_speed.currentIndexChanged.connect(self._speed_changed)
        row.addWidget(self.playback_speed)
        row.addWidget(QtWidgets.QLabel('NWB time (s):'))
        self.time_input = QtWidgets.QDoubleSpinBox()
        self.time_input.setDecimals(6)
        self.time_input.setRange(-1e12, 1e12)
        self.time_input.setKeyboardTracking(False)
        self.time_input.editingFinished.connect(self.seek_time)
        row.addWidget(self.time_input)
        self.go = QtWidgets.QPushButton('Go')
        self.go.clicked.connect(self.seek_time)
        row.addWidget(self.go)
        layout.addLayout(row)
        event_row = QtWidgets.QHBoxLayout()
        event_row.addWidget(QtWidgets.QLabel('Event marker:'))
        self.event_choice = QtWidgets.QComboBox()
        self.event_choice.currentIndexChanged.connect(self.update_event)
        event_row.addWidget(self.event_choice, 1)
        self.event_dot = QtWidgets.QLabel('● Inactive')
        event_row.addWidget(self.event_dot)
        self.event_previous = QtWidgets.QPushButton('Previous event')
        self.event_next = QtWidgets.QPushButton('Next event')
        self.event_previous.clicked.connect(lambda: self.jump_event(-1))
        self.event_next.clicked.connect(lambda: self.jump_event(1))
        event_row.addWidget(self.event_previous)
        event_row.addWidget(self.event_next)
        layout.addLayout(event_row)
        self.status = QtWidgets.QLabel('Playback is read-only. Audio is not played.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._advance)
        self._controls(False)

    def _controls(self, enabled):
        for widget in [self.slider, self.previous, self.play, self.next, self.playback_speed, self.time_input, self.go]:
            widget.setEnabled(enabled)

    def open_objects(self, paths, object_path):
        self.shutdown()
        self.object_path = str(object_path)
        self.file_choice.blockSignals(True)
        self.file_choice.clear()
        for path in paths:
            self.file_choice.addItem(str(path), str(path))
        self.file_choice.blockSignals(False)
        self._file_changed(0)

    def _file_changed(self, index):
        self.shutdown()
        self.source = None
        self.event_data = {}
        self.event_choice.clear()
        self.update_event()
        self._controls(False)
        self.image.clear()
        self._pixmap = None
        if index < 0 or not self.file_choice.count():
            return
        path = self.file_choice.itemData(index)
        self.title.setText(f'{self.object_path}\n{path}')
        try:
            import av  # noqa: F401
            self.source = read_video_metadata(path, self.object_path)
            self.load_events(path)
            self.slider.blockSignals(True)
            self.slider.setRange(0, len(self.source['timestamps'])-1)
            self.slider.setValue(0)
            self.slider.blockSignals(False)
            self.decoder = VideoDecoder(self.source, self)
            self.decoder.frame_ready.connect(self._received)
            self.decoder.failed.connect(self._failed)
            self.decoder.start()
            self._controls(True)
            self.seek(0)
        except ImportError:
            self._failed('Video playback needs PyAV. Install in your NWB environment: python -m pip install av')
        except Exception as exc:
            self._failed(str(exc))

    def seek(self, index):
        self.pause()
        if self.source is not None and self.decoder is not None:
            self.current = min(max(0, int(index)), len(self.source['timestamps'])-1)
            self.status.setText('Loading frame. Long seeks may require decoding intermediate frames.')
            self.decoder.request(self.current)

    def step(self, delta):
        self.seek(self.current+delta)

    def seek_time(self):
        if self.source is None:
            return
        values = self.source['timestamps']
        right = min(len(values)-1, int(np.searchsorted(values, self.time_input.value())))
        left = max(0,right-1)
        index = left if abs(values[left]-self.time_input.value()) < abs(values[right]-self.time_input.value()) else right
        self.seek(index)

    def _received(self, index, rgb):
        if self.sender() is not self.decoder or self.source is None or index != self.current:
            return
        height, width = rgb.shape[:2]
        buffer = np.ascontiguousarray(rgb, dtype=np.uint8).tobytes()
        image = QtGui.QImage(buffer, width, height, width * 3, QtGui.QImage.Format.Format_RGB888).copy()
        self._pixmap = QtGui.QPixmap.fromImage(image)
        self._scale()
        self.slider.blockSignals(True)
        self.slider.setValue(index)
        self.slider.blockSignals(False)
        self.displayed = index
        self.update_event()
        timestamp = float(self.source['timestamps'][index])
        self.time_input.setValue(timestamp)
        self.status.setText(f'Frame {index+1} / {len(self.source["timestamps"])} | NWB time {timestamp:.6f} s | '
                            f'Video elapsed {timestamp-self.source["timestamps"][0]:.3f} s | No audio')
        if self._playing:
            self._schedule()

    def load_events(self, path):
        from pynwb import NWBHDF5IO
        from GUI.events import EventManager
        manager = EventManager()
        errors = []
        with NWBHDF5IO(str(path), 'r', load_namespaces=True) as io:
            nwb = io.read()
            catalog = manager.build_catalog_for_file(str(path), nwbfile=nwb)
            for label, source in catalog.items():
                if source.kind not in {'point', 'interval'}:
                    continue
                try:
                    if source.kind == 'interval':
                        spans = np.asarray(manager.get_spans(nwb, str(path), label), dtype=float).reshape(-1, 2)
                        spans = spans[np.all(np.isfinite(spans), axis=1)]
                        spans = spans[spans[:,1] >= spans[:,0]]
                    else:
                        points = np.asarray(manager.get_points(nwb, str(path), label), dtype=float)
                        points = points[np.isfinite(points)]
                        spans = np.column_stack([points,points])
                    self.event_data[label] = spans.copy()
                except Exception as exc:
                    errors.append(f'{label}: {exc}')
        self.event_choice.blockSignals(True)
        self.event_choice.clear()
        self.event_choice.addItem('None')
        self.event_choice.addItems(sorted(self.event_data))
        preferred = self.event_choice.findText('warning_signal')
        self.event_choice.setCurrentIndex(max(0, preferred))
        self.event_choice.setToolTip('AnnotationSeries and TimeIntervals markers. '+('Unavailable: '+ '; '.join(errors) if errors else ''))
        self.event_choice.blockSignals(False)

    def event_frames(self):
        if self.source is None:
            return np.array([], dtype=int)
        spans = self.event_data.get(self.event_choice.currentText(), np.empty((0,2)))
        times = self.source['timestamps']
        end = times[-1]+(np.median(np.diff(times)) if len(times)>1 else 0)
        onsets = spans[:,0]
        onsets = onsets[(onsets >= times[0]) & (onsets < end)]
        return np.unique(np.searchsorted(times, onsets, side='right')-1)

    def update_event(self, *_):
        active = False
        frames = self.event_frames()
        if self.source is not None and self.displayed < len(self.source['timestamps']):
            times = self.source['timestamps']
            start = times[self.displayed]
            end = (times[self.displayed+1] if self.displayed+1 < len(times)
                   else start+(np.median(np.diff(times)) if len(times)>1 else 0))
            spans = self.event_data.get(self.event_choice.currentText(), np.empty((0,2)))
            point = spans[:,0] == spans[:,1]
            active = bool(np.any((spans[:,0] < end) & np.where(point, spans[:,0] >= start, spans[:,1] > start)))
        self.event_dot.setText('● Active' if active else '● Inactive')
        self.event_dot.setStyleSheet('color: #e67e22; font-weight: bold;' if active else 'color: #808080;')
        self.event_dot.setToolTip('Active when the selected event overlaps this frame interval. The last frame duration is estimated from median frame spacing.')
        self.event_previous.setEnabled(bool(np.any(frames < self.displayed)))
        self.event_next.setEnabled(bool(np.any(frames > self.displayed)))

    def jump_event(self, direction):
        frames = self.event_frames()
        candidates = frames[frames < self.displayed] if direction < 0 else frames[frames > self.displayed]
        if len(candidates):
            self.seek(int(candidates[-1] if direction < 0 else candidates[0]))

    def _scale(self):
        if self._pixmap is not None:
            self.image.setPixmap(self._pixmap)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scale()

    def toggle_play(self):
        if self._playing:
            self.pause()
        elif self.source is not None:
            self._playing = True
            self.play.setText('Pause')
            self._clock = time.monotonic()
            self._time_origin = float(self.source['timestamps'][self.current])
            self._schedule()

    def _schedule(self):
        if self.current+1 >= len(self.source['timestamps']):
            self.pause()
            return
        due = self._clock+(float(self.source['timestamps'][self.current+1])-self._time_origin)/self._playback_rate
        self.timer.start(max(1, int(1000*(due-time.monotonic()))))

    def _speed_changed(self, *_):
        self._playback_rate = float(self.playback_speed.currentData())
        if self._playing and self.source is not None:
            # Reanchor at the visible frame rather than replaying elapsed wall time
            # at the new speed. If decoding is pending, _received schedules next.
            self.timer.stop()
            self._clock = time.monotonic()
            self._time_origin = float(self.source['timestamps'][self.displayed])
            if self.current == self.displayed:
                self._schedule()

    def _advance(self):
        if self._playing and self.decoder is not None:
            self.current += 1
            self.decoder.request(self.current)

    def pause(self):
        self._playing = False
        self.timer.stop()
        self.play.setText('Play')

    def _failed(self, message):
        if self.sender() is not None and isinstance(self.sender(), VideoDecoder) and self.sender() is not self.decoder:
            return
        self.pause()
        self._controls(False)
        self.status.setText(str(message))
        self.image.setText('Video unavailable')

    def hideEvent(self, event):
        self.pause()
        super().hideEvent(event)

    def shutdown(self):
        self.pause()
        if self.decoder is not None:
            decoder, self.decoder = self.decoder, None
            decoder.stop()
            decoder.wait()
            decoder.deleteLater()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
