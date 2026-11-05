from dataclasses import dataclass
from typing import Dict, Optional, Any, List

import numpy as np
from pynwb.misc import AnnotationSeries
from pynwb.epoch import TimeIntervals
from pynwb.behavior import BehavioralEvents, BehavioralEpochs
from pynwb.ecephys import EventDetection


@dataclass(frozen=True)
class EventSource:
    kind: str
    container: str
    key: str
    label: str


class EventManager:
    def __init__(self):
        self.catalog_by_path: Dict[str, Dict[str, EventSource]] = {}
        self.active_by_path: Dict[str, set] = {}
        self.style: Dict[str, Dict[str, Any]] = {}

    def get_style(self, label: str) -> Dict[str, Any]:
        if label not in self.style:
            self.style[label] = {"color": None, "alpha": 0.30}
        return self.style[label]

    def get_active(self, nwb_path: str) -> set:
        return self.active_by_path.setdefault(nwb_path, set())

    def build_catalog_for_file(self, nwb_path: str, nwbfile=None, *, backend=None) -> Dict[str, EventSource]:
        if nwb_path in self.catalog_by_path:
            return self.catalog_by_path[nwb_path]

        catalog: Dict[str, EventSource] = {}

        if nwbfile is None and backend is not None:
            try:
                nwbfile = backend.open(nwb_path)
            except Exception:
                nwbfile = None

        if nwbfile is None:
            self.catalog_by_path[nwb_path] = catalog
            return catalog

        for k, obj in getattr(nwbfile, "acquisition", {}).items():
            if isinstance(obj, AnnotationSeries):
                label = str(k)
                catalog[label] = EventSource("point", "acquisition", str(k), label)

        stim = getattr(nwbfile, "stimulus", None)
        if stim is not None:
            for k, obj in stim.items():
                if isinstance(obj, AnnotationSeries):
                    label = str(k)
                    catalog[label] = EventSource("point", "stimulus", str(k), label)

        intervals = getattr(nwbfile, "intervals", None)
        if intervals is not None:
            for k, obj in intervals.items():
                if isinstance(obj, TimeIntervals):
                    label = str(k)
                    catalog[label] = EventSource("interval", "intervals", str(k), label)

        for mod_name, mod in getattr(nwbfile, "processing", {}).items():
            data_intf = getattr(mod, "data_interfaces", {})
            for k, obj in data_intf.items():
                kk = str(k)
                modn = str(mod_name)

                if isinstance(obj, AnnotationSeries):
                    catalog[kk] = EventSource("point", f"processing:{modn}", kk, kk)
                elif isinstance(obj, TimeIntervals):
                    catalog[kk] = EventSource("interval", f"processing:{modn}", kk, kk)
                elif isinstance(obj, BehavioralEvents):
                    catalog[kk] = EventSource("behavior_point", f"processing:{modn}", kk, kk)
                elif isinstance(obj, BehavioralEpochs):
                    catalog[kk] = EventSource("behavior_interval", f"processing:{modn}", kk, kk)
                elif isinstance(obj, EventDetection):
                    catalog[kk] = EventSource("detection", f"processing:{modn}", kk, kk)

        # units
        units = getattr(nwbfile, "units", None)
        if units is not None:
            try:
                units_df = units.to_dataframe()
            except Exception:
                units_df = None

            try:
                unit_ids = np.asarray(units.id[:]).reshape(-1)
            except Exception:
                if units_df is not None:
                    unit_ids = np.asarray(units_df.index).reshape(-1)
                else:
                    unit_ids = np.array([], dtype=object)

            for row_idx, unit_id in enumerate(unit_ids):
                sorting_project = ""
                if units_df is not None and "sorting_project" in units_df.columns:
                    try:
                        sorting_project = str(units_df.iloc[int(row_idx)]["sorting_project"]).strip()
                    except Exception:
                        sorting_project = ""

                if sorting_project and sorting_project.lower() not in ("nan", "none"):
                    label = f"unit_{unit_id} ({sorting_project})"
                else:
                    label = f"unit_{unit_id}"

                catalog[label] = EventSource(
                    kind="units",
                    container="units",
                    key=str(row_idx),   # row index, not unit id
                    label=label,
                )

        self.catalog_by_path[nwb_path] = catalog
        return catalog

    def list_options(self, nwb_path: str) -> List[str]:
        return sorted(self.catalog_by_path.get(nwb_path, {}).keys())

    def get_source(self, nwbfile, nwb_path: str, label: str) -> Optional[EventSource]:
        return self.catalog_by_path.get(nwb_path, {}).get(label)

    def _get_obj(self, nwbfile, src: EventSource):
        if src.container == "acquisition":
            return nwbfile.acquisition.get(src.key)
        if src.container == "stimulus":
            stim = getattr(nwbfile, "stimulus", None)
            return None if stim is None else stim.get(src.key)
        if src.container == "intervals":
            intervals = getattr(nwbfile, "intervals", None)
            return None if intervals is None else intervals.get(src.key)
        if src.container == "units":
            return getattr(nwbfile, "units", None)

        if src.container.startswith("processing:"):
            mod_name = src.container.split("processing:", 1)[1]
            mod = getattr(nwbfile, "processing", {}).get(mod_name)
            if mod is None:
                return None
            return getattr(mod, "data_interfaces", {}).get(src.key)

        return None

    @staticmethod
    def _annotation_times(ann: AnnotationSeries) -> np.ndarray:
        if getattr(ann, "timestamps", None) is not None:
            try:
                return np.asarray(ann.timestamps[:], dtype=float)
            except Exception:
                return np.asarray(ann.timestamps, dtype=float)

        rate = getattr(ann, "rate", None)
        if rate:
            n = len(ann.data)
            return float(ann.starting_time) + np.arange(n, dtype=float) / float(rate)

        return np.array([], dtype=float)

    @staticmethod
    def _interval_spans(ti: TimeIntervals) -> np.ndarray:
        try:
            df = ti.to_dataframe()
        except Exception:
            return np.empty((0, 2), dtype=float)

        if df is None or df.empty:
            return np.empty((0, 2), dtype=float)

        if "start_time" not in df.columns or "stop_time" not in df.columns:
            return np.empty((0, 2), dtype=float)

        return df[["start_time", "stop_time"]].to_numpy(dtype=float, copy=True)

    @staticmethod
    def _unit_spike_times(units, row_idx: int) -> np.ndarray:
        if units is None:
            return np.array([], dtype=float)

        try:
            return np.asarray(units["spike_times"][row_idx], dtype=float)
        except Exception:
            pass

        try:
            df = units.to_dataframe()
            return np.asarray(df.iloc[int(row_idx)]["spike_times"], dtype=float)
        except Exception:
            return np.array([], dtype=float)

    def get_points(self, nwbfile, nwb_path: str, label: str) -> np.ndarray:
        src = self.get_source(nwbfile, nwb_path, label)
        if src is None:
            return np.array([], dtype=float)

        if src.kind == "point":
            obj = self._get_obj(nwbfile, src)
            if not isinstance(obj, AnnotationSeries):
                return np.array([], dtype=float)
            return self._annotation_times(obj)

        if src.kind == "units":
            units = self._get_obj(nwbfile, src)
            return self._unit_spike_times(units, int(src.key))

        return np.array([], dtype=float)

    def get_spans(self, nwbfile, nwb_path: str, label: str) -> np.ndarray:
        src = self.get_source(nwbfile, nwb_path, label)
        if src is None or src.kind != "interval":
            return np.empty((0, 2), dtype=float)
        obj = self._get_obj(nwbfile, src)
        if not isinstance(obj, TimeIntervals):
            return np.empty((0, 2), dtype=float)
        return self._interval_spans(obj)

    def draw_active(self, ax, nwbfile, nwb_path: str, t_start: float, t_end: float):
        if nwbfile is None:
            return

        cat = self.catalog_by_path.get(nwb_path, {})
        active = self.active_by_path.get(nwb_path, set())
        if not cat or not active:
            return

        used_labels = set()

        for label in sorted(active):
            src = cat.get(label)
            if src is None:
                continue

            style = self.get_style(label)
            color = style.get("color", None)
            alpha = float(style.get("alpha", 0.30))

            if src.kind in ("point", "behavior_point", "detection", "units"):
                if src.kind in ("point", "units"):
                    times = self.get_points(nwbfile, nwb_path, label)
                elif src.kind == "behavior_point":
                    obj = self._get_obj(nwbfile, src)
                    times = []
                    if obj is not None:
                        for ts in obj.time_series.values():
                            if getattr(ts, "timestamps", None) is not None:
                                times.extend(np.asarray(ts.timestamps[:], dtype=float))
                    times = np.asarray(times, dtype=float)
                else:
                    obj = self._get_obj(nwbfile, src)
                    try:
                        times = np.asarray(obj.timestamps[:], dtype=float)
                    except Exception:
                        times = np.array([], dtype=float)

                if times.size == 0:
                    continue

                times = times[(times >= t_start) & (times <= t_end)]
                if times.size == 0:
                    continue

                legend_label = label if label not in used_labels else None
                for i, tt in enumerate(times):
                    ax.axvline(
                        float(tt),
                        alpha=alpha,
                        color=color,
                        linewidth=1.0,
                        label=(legend_label if i == 0 else None),
                    )
                used_labels.add(label)

            elif src.kind in ("interval", "behavior_interval"):
                spans = self.get_spans(nwbfile, nwb_path, label)
                if spans.size == 0:
                    continue

                s = spans[:, 0]
                e = spans[:, 1]
                spans = spans[(e >= t_start) & (s <= t_end)]
                if spans.size == 0:
                    continue

                legend_label = label if label not in used_labels else None
                for i, (a, b) in enumerate(spans):
                    aa = max(float(a), float(t_start))
                    bb = min(float(b), float(t_end))
                    if aa < bb:
                        ax.axvspan(
                            aa, bb,
                            alpha=alpha,
                            color=color,
                            label=(legend_label if i == 0 else None),
                        )
                used_labels.add(label)

        if used_labels:
            ax.legend(loc="upper right", fontsize="small", frameon=True)

    def get_event_centers(self, nwbfile, nwb_path: str, label: str) -> np.ndarray:
        src = self.catalog_by_path.get(nwb_path, {}).get(label)
        if src is None:
            return np.array([], dtype=float)

        if src.kind in ("point", "units"):
            return np.asarray(self.get_points(nwbfile, nwb_path, label), dtype=float)

        if src.kind == "interval":
            spans = self.get_spans(nwbfile, nwb_path, label)
            if spans.size == 0:
                return np.array([], dtype=float)
            return np.asarray(0.5 * (spans[:, 0] + spans[:, 1]), dtype=float)

        return np.array([], dtype=float)