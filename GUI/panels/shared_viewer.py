"""One recording, one NWB time cursor, multiple read-only signal/video views."""
from pathlib import Path
import time
import h5py
import numpy as np
import pyqtgraph as pg
from GUI.qt_compat import QtCore, QtWidgets
from GUI.panels.pose_video import TrackingVideoPanel,pose_metadata


class LinkedVideo(TrackingVideoPanel):
    presented = QtCore.Signal(int)

    def _received(self, index, rgb):
        valid = self.sender() is self.decoder and index == self.current
        super()._received(index, rgb)
        if valid:
            self.presented.emit(index)


def lower_bound(dataset, value):
    lo, hi = 0, len(dataset)
    while lo < hi:
        mid = (lo+hi)//2
        if float(dataset[mid]) < value:
            lo = mid+1
        else:
            hi = mid
    return lo


class SignalTrack(QtWidgets.QWidget):
    def __init__(self, owner, path):
        super().__init__()
        self.owner, self.path = owner, path
        self.overlays = []
        with h5py.File(owner.nwb_path, 'r') as file:
            group = file[path]
            self.pose=pose_metadata(group) is not None
            data = group['data']
            if len(data.shape) not in (1,2) or data.dtype.kind not in 'fiu':
                raise ValueError('Choose a numeric TimeSeries with samples × channels.')
            self.count = len(data)
            if not self.count:
                raise ValueError('Signal is empty.')
            self.channels = data.shape[1] if len(data.shape)==2 else 1
            self.explicit = 'timestamps' in group
            if self.explicit:
                if len(group['timestamps']) != self.count:
                    raise ValueError('Signal timestamps and samples have different lengths.')
                self.start, self.end = float(group['timestamps'][0]),float(group['timestamps'][-1])
            else:
                self.start = float(group['starting_time'][()])
                self.rate = float(group['starting_time'].attrs['rate'])
                if not np.isfinite(self.rate) or self.rate <= 0:
                    raise ValueError('Invalid sampling rate.')
                self.end = self.start+(self.count-1)/self.rate
            self.unit = str(data.attrs.get('unit',''))
        layout=QtWidgets.QVBoxLayout(self)
        row=QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel(path),1)
        row.addWidget(QtWidgets.QLabel('Channel'))
        self.channel=QtWidgets.QSpinBox();self.channel.setRange(0,self.channels-1)
        self.channel.valueChanged.connect(lambda: owner.refresh())
        row.addWidget(self.channel)
        if self.pose:
            self.channel.hide();row.itemAt(1).widget().hide()
            row.addWidget(QtWidgets.QLabel('X: blue (left) · Y: red (right)'))
        close=QtWidgets.QPushButton('Remove');close.clicked.connect(lambda:owner.remove_track(self));row.addWidget(close)
        layout.addLayout(row)
        self.plot=pg.PlotWidget()
        self.plot.setBackground("w")
        self.plot.showGrid(x=False,y=False)
        self.plot.setMinimumHeight(140)
        self.plot.setLabel('bottom','NWB time',units='s')
        self.plot.setLabel('left',self.unit)
        self.plot.setMouseEnabled(x=True,y=True)
        self.plot.getViewBox().setMouseMode(pg.ViewBox.PanMode)
        self.plot.getViewBox().sigRangeChangedManually.connect(self.range_changed)
        self.plot.setToolTip('Left-drag to pan; wheel/right-drag to zoom. Ctrl+click or drag the orange cursor to seek.')
        self.curve=self.plot.plot(pen=pg.mkPen('#2471a3' if self.pose else 'k',width=1))
        if self.pose:
            self.plot.setLabel('left','X coordinate',units='pixels',color='#2471a3')
            self.plot.showAxis('right');self.plot.setLabel('right','Y coordinate',units='pixels',color='#c0392b')
            self.y_view=pg.ViewBox();self.plot.scene().addItem(self.y_view)
            self.plot.getAxis('right').linkToView(self.y_view);self.y_view.setXLink(self.plot.getViewBox())
            self.y_view.setMouseEnabled(x=False,y=False)
            self.y_curve=pg.PlotDataItem(pen=pg.mkPen('#c0392b',width=1));self.y_view.addItem(self.y_curve)
            self.plot.getViewBox().sigResized.connect(self.resize_y_view);self.resize_y_view()
        self.cursor=pg.InfiniteLine(angle=90,movable=True,pen=pg.mkPen('#e67e22',width=2))
        self.plot.addItem(self.cursor)
        self.cursor.sigPositionChangeFinished.connect(lambda:owner.seek(self.cursor.value()))
        self.plot.scene().sigMouseClicked.connect(self.clicked)
        layout.addWidget(self.plot)

    def resize_y_view(self):
        self.y_view.setGeometry(self.plot.getViewBox().sceneBoundingRect())
        self.y_view.linkedViewChanged(self.plot.getViewBox(),self.y_view.XAxis)

    def range_changed(self, axes):
        if axes[0]:
            self.owner.pan_range(*self.plot.getViewBox().viewRange()[0])

    def clicked(self,event):
        if (event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier) and event.button()==QtCore.Qt.MouseButton.LeftButton and self.plot.sceneBoundingRect().contains(event.scenePos()):
            self.owner.seek(self.plot.plotItem.vb.mapSceneToView(event.scenePos()).x())

    def refresh(self,left,right):
        with h5py.File(self.owner.nwb_path,'r') as file:
            group=file[self.path];data=group['data']
            if self.explicit:
                ts=group['timestamps'];a=lower_bound(ts,left);b=lower_bound(ts,right)
            else:
                a=max(0,int(np.ceil((left-self.start)*self.rate)))
                b=min(self.count,int(np.ceil((right-self.start)*self.rate)))
            a,b=min(self.count,max(0,a)),min(self.count,max(0,b))
            # Read only this time window; cap transferred samples for large windows.
            step=max(1,int(np.ceil(max(0,b-a)/200000)))
            values=np.asarray(data[a:b:step,:2] if self.pose else data[a:b:step] if data.ndim==1 else data[a:b:step,self.channel.value()],dtype=float)
            x=np.asarray(group['timestamps'][a:b:step]) if self.explicit else self.start+np.arange(a,b,step)/self.rate
            conversion=float(data.attrs.get('conversion',1));offset=float(data.attrs.get('offset',0))
            if 'channel_conversion' in group:
                conversion*=float(group['channel_conversion'][self.channel.value()])
            values=values*conversion+offset
        self.curve.setData(x,values[:,0] if self.pose else values,autoDownsample=True,downsampleMethod='peak',connect='finite')
        if self.pose:self.y_curve.setData(x,values[:,1],autoDownsample=True,downsampleMethod='peak',connect='finite')
        self.plot.setXRange(left,right,padding=0)
        for item in self.overlays:self.plot.removeItem(item)
        self.overlays=[]
        spans=self.owner.selected_events()
        for start,end in spans[(spans[:,0]<right)&(spans[:,1]>=left)][:1000]:
            if end>start:
                item=pg.LinearRegionItem([start,end],movable=False,brush=(230,126,34,45),pen=pg.mkPen(None))
            else:item=pg.InfiniteLine(start,pen=pg.mkPen('#e67e22'))
            self.plot.addItem(item);self.overlays.append(item)
        self.cursor.setValue(self.owner.current_time)


class SharedViewer(QtWidgets.QWidget):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.nwb_path=None;self.tracks=[];self.current_time=0.;self.video_path=None
        self.pending=None;self.playing=False;self.left=0.;self.events={}
        self.reload_timer=QtCore.QTimer(self);self.reload_timer.setSingleShot(True);self.reload_timer.setInterval(100)
        self.reload_timer.timeout.connect(self.refresh)
        self.timer=QtCore.QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self.advance)
        layout=QtWidgets.QVBoxLayout(self)
        self.title=QtWidgets.QLabel('Shared NWB viewer');layout.addWidget(self.title)
        self.splitter=QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical);layout.addWidget(self.splitter,1)
        self.video=LinkedVideo(self);self.video.presented.connect(self.presented)
        self.video.hide();self.splitter.addWidget(self.video)
        # Shared controls below replace the standalone player controls.
        for widget in [self.video.slider,self.video.previous,self.video.next,self.video.play,self.video.playback_speed,self.video.time_input,self.video.go,
                       self.video.event_choice,self.video.event_previous,self.video.event_next,self.video.file_choice]:widget.hide()
        for label in self.video.findChildren(QtWidgets.QLabel):
            if label.text() in {'NWB time (s):','Event marker:','Speed'}:label.hide()
        self.track_host=QtWidgets.QWidget();self.track_layout=QtWidgets.QVBoxLayout(self.track_host)
        scroll=QtWidgets.QScrollArea();scroll.setWidgetResizable(True);scroll.setMinimumHeight(220);scroll.setWidget(self.track_host);self.splitter.addWidget(scroll)
        self.splitter.setStretchFactor(0,1);self.splitter.setStretchFactor(1,2)
        row=QtWidgets.QHBoxLayout();layout.addLayout(row)
        self.play_button=QtWidgets.QPushButton('Play');self.play_button.clicked.connect(self.toggle)
        for name,delta in [('Previous frame',-1),('Next frame',1)]:
            button=QtWidgets.QPushButton(name);button.clicked.connect(lambda _,d=delta:self.step(d));row.addWidget(button)
        row.addWidget(self.play_button)
        row.addWidget(QtWidgets.QLabel('NWB time (s)'))
        self.time_box=QtWidgets.QDoubleSpinBox();self.time_box.setRange(-1e10,1e10);self.time_box.setDecimals(6);self.time_box.setKeyboardTracking(False)
        self.time_box.editingFinished.connect(lambda:self.seek(self.time_box.value()));row.addWidget(self.time_box)
        row.addWidget(QtWidgets.QLabel('Speed'))
        self.speed=QtWidgets.QComboBox();self.speed.addItems(['0.1','0.25','0.5','0.75','1','1.5','2','4']);self.speed.setCurrentText('1');self.speed.setToolTip('Playback multiplier; shared video and data remain synchronized.');row.addWidget(self.speed)
        row2=QtWidgets.QHBoxLayout();layout.addLayout(row2)
        row2.addWidget(QtWidgets.QLabel('Window (s)'))
        self.duration=QtWidgets.QDoubleSpinBox();self.duration.setDecimals(6);self.duration.setRange(.000001,1e10);self.duration.setValue(10);self.duration.valueChanged.connect(lambda:self.refresh());row2.addWidget(self.duration)
        self.follow=QtWidgets.QCheckBox('Follow cursor');row2.addWidget(self.follow)
        self.marker=QtWidgets.QComboBox();self.marker.currentIndexChanged.connect(self.event_changed);row2.addWidget(self.marker,1)
        for name,delta in [('Previous event',-1),('Next event',1)]:
            button=QtWidgets.QPushButton(name);button.clicked.connect(lambda _,d=delta:self.jump_event(d));row2.addWidget(button)
        self.remove_video=QtWidgets.QPushButton('Remove video');self.remove_video.clicked.connect(self.clear_video);row2.addWidget(self.remove_video)
        self.status=QtWidgets.QLabel('Right-click data in Content and choose Add to current view.');self.status.setWordWrap(True);layout.addWidget(self.status)

    def add_object(self,nwb_path,path):
        nwb_path=str(Path(nwb_path).resolve())
        if self.nwb_path and nwb_path!=self.nwb_path:
            raise ValueError('Shared viewing uses one NWB recording. Choose Open in new shared view for another file.')
        with h5py.File(nwb_path,'r') as file:is_video='external_file' in file[path]
        first=self.nwb_path is None
        self.nwb_path=nwb_path;self.title.setText(nwb_path)
        if is_video:
            self.pause();self.video.open_objects([nwb_path],path)
            if self.video.source is None:raise ValueError(self.video.status.text())
            self.video.decoder.failed.connect(self.video_failed)
            self.video_path=path;self.video.show()
            if first:self.current_time=float(self.video.source['timestamps'][0])
        elif not any(track.path==path for track in self.tracks):
            track=SignalTrack(self,path);self.tracks.append(track);self.track_layout.addWidget(track)
            if first:self.current_time=track.start
        self.video.load_events(nwb_path);self.events=self.video.event_data
        old=self.marker.currentText();self.marker.blockSignals(True);self.marker.clear();self.marker.addItem('None');self.marker.addItems(sorted(self.events))
        self.marker.setCurrentText(old if old in self.events else 'warning_signal' if 'warning_signal' in self.events else 'None');self.marker.blockSignals(False)
        self.left=self.current_time;self.event_changed();self.seek(self.current_time)
        if self.video_path and self.tracks:self.splitter.setSizes([300,500])

    def video_failed(self, message):
        self.pending=None;self.pause();self.status.setText('Video decoding failed: '+str(message))

    def clear_video(self):
        self.pause();self.video.shutdown();self.video.source=None;self.video_path=None;self.video.hide();self.pending=None

    def remove_track(self,track):
        self.tracks.remove(track);self.track_layout.removeWidget(track);track.deleteLater()

    def selected_events(self):
        return self.events.get(self.marker.currentText(),np.empty((0,2)))

    def event_changed(self,*_):
        self.video.event_choice.setCurrentText(self.marker.currentText());self.refresh()

    def pan_range(self, left, right):
        if not np.isfinite(left) or not np.isfinite(right) or right <= left:
            return
        self.pause()
        self.follow.setChecked(False)
        self.left=float(left)
        self.duration.blockSignals(True)
        self.duration.setValue(float(right-left))
        self.duration.blockSignals(False)
        for track in self.tracks:
            track.plot.setXRange(left,right,padding=0)
        self.reload_timer.start()

    def refresh(self, recenter=False):
        if self.follow.isChecked():self.left=self.current_time-self.duration.value()/2
        elif recenter and not self.left<=self.current_time<self.left+self.duration.value():self.left=self.current_time
        try:
            for track in self.tracks:track.refresh(self.left,self.left+self.duration.value())
        except Exception as exc:self.pause();self.status.setText(str(exc))

    def seek(self,value):
        self.pause();self.request_time(float(value))

    def request_time(self,value):
        if self.video_path and self.video.source is not None:
            ts=self.video.source['timestamps'];end=ts[-1]+(np.median(np.diff(ts)) if len(ts)>1 else 0)
            if ts[0]<=value<end:
                index=int(np.searchsorted(ts,value,side='right')-1)
                self.pending=(index,value)
                self.video.seek(index)
                self.status.setText('Loading synchronized frame…')
                return
            self.video._pixmap=None;self.video.image.setText('No video at this NWB time');self.video.event_dot.setText('● No video')
        self.pending=None;self.commit_time(value)

    def presented(self,index):
        if self.pending is not None and self.pending[0]==index:
            value=self.pending[1];self.pending=None;self.commit_time(value)
        elif self.pending is None and self.video_path:
            # Ignore stale decoder results after an out-of-coverage seek.
            self.video._pixmap=None;self.video.image.setText('No video at this NWB time')

    def commit_time(self,value):
        old_left=self.left;self.current_time=value;self.time_box.setValue(value)
        if self.follow.isChecked() or not self.left<=value<self.left+self.duration.value():self.refresh(recenter=True)
        else:
            for track in self.tracks:track.cursor.setValue(value)
        self.status.setText(f'Shared NWB time: {value:.6f} s. Signal display may be decimated for long windows.')
        if self.playing:self.timer.start(max(1,int(1000*self.next_interval()/float(self.speed.currentText()))))

    def next_interval(self):
        if self.video_path:
            ts=self.video.source['timestamps']
            if self.current_time < ts[0]:return min(.04,float(ts[0]-self.current_time))
            i=int(np.searchsorted(ts,self.current_time+1e-9))
            if i<len(ts):return max(.001,float(ts[i]-self.current_time))
        return .04

    def advance(self):
        ends=[t.end for t in self.tracks]
        if self.video_path:ends.append(float(self.video.source['timestamps'][-1]))
        if not ends or self.current_time>=max(ends):self.pause();return
        self.request_time(min(max(ends),self.current_time+self.next_interval()))

    def toggle(self):
        if self.playing:self.pause()
        else:
            self.playing=True;self.play_button.setText('Pause');self.advance()

    def pause(self):
        self.playing=False;self.timer.stop();self.play_button.setText('Play')

    def step(self,delta):
        if self.video_path:
            ts=self.video.source['timestamps'];i=int(np.searchsorted(ts,self.current_time,side='left'))
            if delta<0:i-=1
            elif i<len(ts) and ts[i]<=self.current_time+1e-9:i+=1
            self.seek(float(ts[min(len(ts)-1,max(0,i))]))
        else:self.seek(self.current_time+delta*.04)

    def jump_event(self,delta):
        spans=self.selected_events();onsets=np.sort(spans[:,0]);values=onsets[onsets<self.current_time-1e-9] if delta<0 else onsets[onsets>self.current_time+1e-9]
        if len(values):self.seek(float(values[-1] if delta<0 else values[0]))

    def hideEvent(self,event):
        self.pause();super().hideEvent(event)

    def shutdown(self):
        self.reload_timer.stop();self.pause();self.video.shutdown()
