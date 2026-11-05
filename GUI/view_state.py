# GUI/view_state.py
from __future__ import annotations
import numpy as np


class ViewState:
    def __init__(self, window_s: float = 5.0):
        self.global_window_s = float(window_s)
        self.global_t_start = None
        self.global_start_frac = 0.0

    @property
    def window_s(self) -> float:
        return float(self.global_window_s)

    @window_s.setter
    def window_s(self, value: float):
        self.global_window_s = float(value)

    def _timestamps_array(self, ts):
        if getattr(ts, "timestamps", None) is None:
            return None
        try:
            return np.asarray(ts.timestamps[:], dtype=float)
        except Exception:
            try:
                return np.asarray(ts.timestamps, dtype=float)
            except Exception:
                return None

    def update_from_indices(self, ts, start_idx: int, total_n: int, view_n: int):
        max_start = max(1, int(total_n) - int(view_n))
        self.global_start_frac = float(np.clip(float(start_idx) / float(max_start), 0.0, 1.0))

        t = self._timestamps_array(ts)
        if t is not None and len(t) > 0:
            try:
                idx = int(np.clip(start_idx, 0, len(t) - 1))
                self.global_t_start = float(t[idx])
                return
            except Exception:
                pass

        if getattr(ts, "rate", None):
            try:
                self.global_t_start = float(ts.starting_time) + float(start_idx) / float(ts.rate)
                return
            except Exception:
                pass

        self.global_t_start = None

    def compute_start_idx(self, ts, total_n: int, view_n: int, filtered_timestamp=None) -> int:
        total_n = int(total_n)
        view_n = int(view_n)
        max_start = max(0, total_n - view_n)
        
        if filtered_timestamp is None:
            t = self._timestamps_array(ts)
        else:
            t = filtered_timestamp
            
        if t is not None and len(t) > 0 and self.global_t_start is not None:
            try:
                idx = int(np.searchsorted(t, float(self.global_t_start), side="left"))
                return int(np.clip(idx, 0, max_start))
            except Exception:
                pass

        if getattr(ts, "rate", None) and (self.global_t_start is not None):
            try:
                start = int(round((float(self.global_t_start) - float(ts.starting_time)) * float(ts.rate)))
                return int(np.clip(start, 0, max_start))
            except Exception:
                pass

        start = int(round(float(self.global_start_frac) * float(max_start)))
        return int(np.clip(start, 0, max_start))

    def indices_from_state(self, ts, total_n: int):
        view_n = None

        t = self._timestamps_array(ts)
        if t is not None and len(t) >= 2:
            try:
                target_end = float(t[0]) + float(self.global_window_s)
                view_n = int(np.searchsorted(t, target_end, side="right"))
                view_n = max(1, min(view_n, int(total_n)))
            except Exception:
                view_n = None

        elif getattr(ts, "rate", None):
            try:
                view_n = int(round(float(self.global_window_s) * float(ts.rate)))
                view_n = max(1, min(view_n, int(total_n)))
            except Exception:
                view_n = None

        start_idx = self.compute_start_idx(ts, int(total_n), int(view_n) if view_n is not None else 1)
        return start_idx, view_n