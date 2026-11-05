from __future__ import annotations

import uuid
from typing import Optional, Literal, Union

import numpy as np
from pynwb import NWBHDF5IO


OpenMode = Literal["r", "r+"]


def get_data_by_nwb_path(nwbfile, nwb_data_path: str):
    s = str(nwb_data_path).strip().strip("/")
    if not s:
        return None

    parts = s.split("/")

    if parts[0] == "trials":
        return getattr(nwbfile, "trials", None)
    
    elif parts[0] == "units":
        return getattr(nwbfile, "units", None)

    elif parts[0] == "acquisition" and len(parts) >= 2:
        key = "/".join(parts[1:])
        return nwbfile.acquisition.get(key)

    elif parts[0] == "processing" and len(parts) >= 3:
        module = parts[1]
        key = "/".join(parts[2:])
        mod = nwbfile.processing.get(module)
        if mod is None:
            return None
        return mod.data_interfaces.get(key)

    elif parts[0] == "stimulus" and len(parts) >= 2:
        key = "/".join(parts[1:])
        return nwbfile.stimulus.get(key)

    elif parts[0] == "intervals" and len(parts) >= 2:
        key = "/".join(parts[1:])
        return nwbfile.intervals.get(key)

    return None

class NWBBackend:
    """
    Single owner of NWBHDF5IO handle.

    Important design rule:
      - GUI code should not keep NWB objects (TimeSeries, NWBFile, datasets)
        across open/close cycles.
      - Store stable identifiers (ts_path) and resolve just-in-time.
    """

    def __init__(self):
        self.io: Optional[NWBHDF5IO] = None
        self.nwbfile = None
        self.path: Optional[str] = None
        self.mode: Optional[OpenMode] = None

        self._lock_id: Optional[str] = None
        self._lock_owner: Optional[str] = None

        self._busy_write_path: Optional[str] = None
        self._busy_write_lock_id: Optional[str] = None

    # ------------------- locking -------------------
    def is_open(self, path):
        return self.path is not None and self.path == path
    
    def is_locked(self) -> bool:
        return self._lock_id is not None

    def current_lock_id(self) -> Optional[str]:
        return self._lock_id

    def lock(self, *, owner: str = "") -> str:
        if self._lock_id is None:
            self._lock_id = uuid.uuid4().hex
            self._lock_owner = str(owner or "")
        return self._lock_id

    def unlock(self, lock_id: str) -> bool:
        if self._lock_id is None:
            return True
        if str(lock_id) != str(self._lock_id):
            return False
        self._lock_id = None
        self._lock_owner = None
        return True

    def _check_can_close(self, lock_id: Optional[str]) -> None:
        if self._lock_id is None:
            return
        if lock_id is None or str(lock_id) != str(self._lock_id):
            raise RuntimeError(
                "NWB file is locked and cannot be closed. "
                "Call unlock(lock_id) first or pass lock_id to close()."
            )

    # ------------------- IO lifecycle -------------------

    def close(self, *, lock_id: Optional[str] = None) -> None:
        self._check_can_close(lock_id)
    
        if self.io is not None:
            self.io.close()
            
    
        self.io = None
        self.nwbfile = None
        self.path = None
        self.mode = None
    
        # clear busy write state
        self._busy_write_path = None
        self._busy_write_lock_id = None
    
        # if caller closed with the active lock, release it too
        if self._lock_id is not None:
            if lock_id is not None and str(lock_id) == str(self._lock_id):
                self._lock_id = None
                self._lock_owner = None

    def open(
        self,
        nwb_path: str,
        opening_mode: OpenMode = "r",
        *,
        lock: bool = False,
        lock_owner: str = "",
        lock_id: Optional[str] = None,
    ):
        nwb_path = str(nwb_path)
    
        # same file already open
        if self.is_open(nwb_path):
            # reuse if mode is compatible
            if self.mode == opening_mode:
                if lock:
                    lid = self.lock(owner=lock_owner)
                    return self.nwbfile, lid
                return self.nwbfile
    
            # upgrade r -> r+ or any other mode change requires close/reopen
            self.close(lock_id=lock_id)
    
        # different file already open
        elif self.io is not None:
            self.close(lock_id=lock_id)
    
        # open fresh
        self.io = NWBHDF5IO(nwb_path, opening_mode, load_namespaces=True)
        self.nwbfile = self.io.read()
        self.path = nwb_path
        self.mode = opening_mode
    
        if lock:
            lid = self.lock(owner=lock_owner)
            return self.nwbfile, lid
    
        return self.nwbfile

    def write(self, nwbfile=None, *, lock_id: Optional[str] = None, close_after: bool = True) -> None:
        if self.io is None or self.nwbfile is None or self.path is None:
            raise RuntimeError("No NWB file is open")

        if self.mode != "r+":
            raise RuntimeError(f"File is not open in write mode (current mode: {self.mode})")

        if self._lock_id is not None:
            self._check_can_close(lock_id)

        self.io.write(nwbfile if nwbfile is not None else self.nwbfile)

        if close_after:
            self.close(lock_id=lock_id)

    def begin_write(self, path: str, *, lock_id: str) -> None:
        if self._lock_id is None or str(lock_id) != str(self._lock_id):
            raise RuntimeError("begin_write requires the current lock_id (open(..., lock=True)).")
        self._busy_write_path = str(path)
        self._busy_write_lock_id = str(lock_id)

    def end_write(self, *, lock_id: str) -> None:
        if self._busy_write_path is None:
            return
        if str(lock_id) != str(self._busy_write_lock_id or ""):
            raise RuntimeError("end_write called with wrong lock_id.")
        self._busy_write_path = None
        self._busy_write_lock_id = None

    # ------------------- convenience methods -------------------

    @staticmethod
    def get_total_n(ts) -> int:
        shape = ts.data.shape
        return int(shape[0]) if len(shape) > 1 else int(len(ts.data))

    @staticmethod
    def get_channel_names(ts):
        electrode_region = getattr(ts, "electrodes", None)
        if electrode_region is None:
            electrode_region = getattr(ts, "source_channels", None)
        if electrode_region is not None:
            reg = electrode_region
            table = reg.table
            rows = np.asarray(reg.data[:], dtype=int)
            for col in ("contact_ids", "contact_id", "label", "id"):
                if col in table.colnames:
                    vals = table[col].data[rows]
                    return [str(v) for v in vals]
            return [f"elec[{i}]" for i in rows.tolist()]

        shape = tuple(ts.data.shape)
        if len(shape) > 2 and all(int(size) == 1 for size in shape[2:]):
            shape = shape[:2]
        if len(shape) == 1:
            return ["0"]
        if len(shape) == 2:
            return [str(i) for i in range(int(shape[1]))]
        raise NotImplementedError(f"Unsupported data shape {shape}")

    def resolve_timeseries(self, nwbfile, ts_path: str):
        """
        ts_path examples:
          acquisition:<key>
          stimulus:<key>
          processing:<module>:<key>
        """
        if not ts_path:
            raise KeyError("Empty ts_path")

        parts = ts_path.split(":")
        if parts[0] == "acquisition" and len(parts) == 2:
            return nwbfile.acquisition[parts[1]]

        if parts[0] == "stimulus" and len(parts) == 2:
            stim = getattr(nwbfile, "stimulus", None)
            if stim is None:
                raise KeyError("No stimulus container in this NWB file")
            return stim[parts[1]]

        if parts[0] == "processing" and len(parts) == 3:
            mod, key = parts[1], parts[2]
            return nwbfile.processing[mod].data_interfaces[key]

        raise KeyError(f"Unrecognized ts_path: {ts_path}")

    def get_timeseries(self, nwb_path: str, ts_path: str, opening_mode: OpenMode = "r"):
        nwbfile = self.open(nwb_path, opening_mode)
        return self.resolve_timeseries(nwbfile, ts_path)
    
    def get_trials_df(self, nwb_path: str, ts_path: str, opening_mode: OpenMode = "r"):
        nwbfile = self.open(nwb_path, opening_mode)
        return nwbfile.trials.to_dataframe()
    
    def read_ts_indices(
        self,
        ts_path: Union[str, object],
        indices,
        *,
        nwb_path: Optional[str] = None,
        ch: Optional[int] = None,
        mode: str = "single",
    ):
        """
        Read arbitrary sample indices robustly, always resolving the TimeSeries
        from a stable ts_path.
    
        Prefer passing ts_path as a string. If an object is passed, we try to read
        ts_path from obj._gui_path for backwards compatibility.
        """
        if not isinstance(ts_path, str):
            ts_path = getattr(ts_path, "_gui_path", None)
            if not isinstance(ts_path, str) or not ts_path:
                raise RuntimeError(
                    "read_ts_indices expects a ts_path string (acquisition:..., stimulus:..., processing:...:...)."
                )
    
        if nwb_path is None:
            if not self.path:
                raise RuntimeError("Backend has no open path. Pass nwb_path explicitly.")
            nwb_path = self.path
    
        idx = np.asarray(indices, dtype=int).reshape(-1)
    
        def _read(ts):
            data = ts.data
    
            if data.ndim == 1:
                arr = np.asarray(data[idx], dtype=float)
            else:
                if mode == "avg":
                    block = np.asarray(data[idx, :], dtype=float)
                    while block.ndim > 2 and block.shape[-1] == 1:
                        block = block[..., 0]
                    if block.ndim > 2:
                        raise NotImplementedError(
                            f"Unsupported data shape {tuple(data.shape)}. "
                            "Only singleton dimensions after time and channels are supported."
                        )
                    arr = np.nanmean(block, axis=1)
                else:
                    if ch is None:
                        raise ValueError("ch must be provided for mode='single' on 2D data.")
                    arr = np.asarray(data[idx, int(ch)], dtype=float)
                    while arr.ndim > 1 and arr.shape[-1] == 1:
                        arr = arr[..., 0]
                    if arr.ndim > 1:
                        raise NotImplementedError(
                            f"Unsupported data shape {tuple(data.shape)}. "
                            "Only singleton dimensions after time and channels are supported."
                        )
    
            scale = float(getattr(ts, "conversion", 1.0) or 1.0)
            offset = float(getattr(ts, "offset", 0.0) or 0.0)
            return arr * scale + offset
    
        try:
            ts = self.get_timeseries(str(nwb_path), str(ts_path), "r")
            return _read(ts)
        except RuntimeError:
            try:
                self.close()
            except Exception:
                pass
            ts = self.get_timeseries(str(nwb_path), str(ts_path), "r")
            return _read(ts)
