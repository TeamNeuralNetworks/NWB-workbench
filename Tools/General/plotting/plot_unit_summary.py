# -*- coding: utf-8 -*-
"""
Unit summary plotting with optional event alignment.

This version keeps the same general figure layout as the original
plot_unit_summary.py, but it does NOT use external raster package, external rate package, external spike-train package, or
external units package. Raster, PSTH, and smoothed firing-rate/z-score panels are computed
with NumPy/SciPy/Matplotlib only.
"""
from __future__ import annotations

import multiprocessing as mp
import os
from pathlib import Path
import matplotlib
matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt
plt.ioff()
from matplotlib.gridspec import GridSpec
import numpy as np
from scipy import stats
from scipy.ndimage import gaussian_filter1d
from tqdm import tqdm

import spikeinterface.widgets as sw
from spikeinterface.core import get_template_amplitudes

# -----------------------------------------------------------------------------
# SpikeInterface extension helpers
# -----------------------------------------------------------------------------

def load_or_compute_extension(analyzer, extension_list, extension_params=None, job_kwargs=None):
    """Compute missing SortingAnalyzer extensions safely."""
    if not isinstance(extension_list, (list, tuple)):
        extension_list = [extension_list]

    to_compute = []
    for extension_name in extension_list:
        try:
            ext = analyzer.get_extension(extension_name)
        except Exception:
            ext = None
        if ext is None:
            to_compute.append(extension_name)

    if not to_compute:
        return

    use_params = False
    if extension_params is not None:
        for name in to_compute:
            if name in extension_params:
                use_params = True
                break

    job_kwargs = dict(job_kwargs or {})
    if not use_params:
        try:
            analyzer.compute(to_compute, verbose=True, **job_kwargs)
        except TypeError:
            analyzer.compute(to_compute, verbose=True)
    else:
        try:
            analyzer.compute(to_compute, verbose=True, extension_params=extension_params, **job_kwargs)
        except TypeError:
            analyzer.compute(to_compute, verbose=True, extension_params=extension_params)


def _safe_count_spikes(analyzer, unit_id):
    try:
        return int(analyzer.sorting.count_num_spikes_per_unit()[unit_id])
    except Exception:
        try:
            return int(len(analyzer.sorting.get_unit_spike_train(unit_id)))
        except Exception:
            return 0


def _get_sampling_frequency(analyzer):
    try:
        sf = float(getattr(analyzer, "sampling_frequency"))
        if np.isfinite(sf) and sf > 0:
            return sf
    except Exception:
        pass

    try:
        sf = float(analyzer.recording.get_sampling_frequency())
        if np.isfinite(sf) and sf > 0:
            return sf
    except Exception:
        pass

    return 1.0


# -----------------------------------------------------------------------------
# Spike-train cutting helpers
# -----------------------------------------------------------------------------

def make_event_aligned_spike_trains(
    analyzer,
    unit_id,
    event_frames,
    pad_before_s,
    pad_after_s,
):
    """
    Return a list of NumPy arrays, one array per event.

    Each array contains relative spike times in seconds, with the event at 0.
    event_frames must be in the same frame coordinate system as analyzer.sorting.
    """
    sf = _get_sampling_frequency(analyzer)
    before = max(0.0, float(pad_before_s))
    after = max(0.0, float(pad_after_s))
    if before <= 0 and after <= 0:
        after = 1.0

    event_frames = np.asarray(event_frames if event_frames is not None else [], dtype=float).reshape(-1)
    event_frames = event_frames[np.isfinite(event_frames)]

    spike_frames = np.asarray(analyzer.sorting.get_unit_spike_train(unit_id), dtype=float).reshape(-1)
    spike_frames = spike_frames[np.isfinite(spike_frames)]

    trains = []
    for ev_frame in event_frames:
        start_frame = ev_frame - before * sf
        stop_frame = ev_frame + after * sf
        mask = (spike_frames >= start_frame) & (spike_frames <= stop_frame)
        rel_s = (spike_frames[mask] - ev_frame) / sf
        trains.append(np.asarray(rel_s, dtype=float))

    return trains


def make_fixed_length_spike_trains(analyzer, unit_id, trial_len=None):
    """
    Return a list of NumPy arrays, one array per fixed-length trial/window.

    Spike times are in
    seconds and relative to the start of each trial/window.
    """
    sf = _get_sampling_frequency(analyzer)

    if trial_len is None:
        try:
            trial_len = float(analyzer.recording.get_duration())
        except Exception:
            trial_len = 1.0
    trial_len = max(1e-9, float(trial_len))

    spike_times = np.asarray(analyzer.sorting.get_unit_spike_train(unit_id), dtype=float).reshape(-1) / sf
    spike_times = spike_times[np.isfinite(spike_times)]

    try:
        duration = float(analyzer.recording.get_duration())
    except Exception:
        duration = float(np.nanmax(spike_times)) if spike_times.size else trial_len
    duration = max(trial_len, duration)

    n_trials = max(1, int(np.ceil(duration / trial_len)))
    trains = []
    for i in range(n_trials):
        t0 = i * trial_len
        t1 = t0 + trial_len
        rel = spike_times[(spike_times >= t0) & (spike_times < t1)] - t0
        trains.append(np.asarray(rel, dtype=float))

    return trains


# -----------------------------------------------------------------------------
# Manual raster / PSTH / rate helpers, no external raster package/external rate package
# -----------------------------------------------------------------------------

def _make_bin_edges(x_start, x_stop, bin_size_s=0.05):
    x_start = float(x_start)
    x_stop = float(x_stop)
    bin_size_s = max(1e-6, float(bin_size_s))
    if x_stop <= x_start:
        x_stop = x_start + bin_size_s

    n_bins = max(1, int(np.ceil((x_stop - x_start) / bin_size_s)))
    edges = x_start + np.arange(n_bins + 1, dtype=float) * bin_size_s
    edges[-1] = x_stop
    centers = 0.5 * (edges[:-1] + edges[1:])
    return edges, centers


def _rate_matrix_from_spike_trains(
    spike_train_list,
    x_start,
    x_stop,
    bin_size_s=0.05,
    smooth_sigma_s=0.10,
):
    """
    Create a trials x time-bins firing-rate matrix in Hz.

    Gaussian smoothing is applied in bins. This is the replacement for
    external rate package.statistics.instantaexternal spike-train packageus_rate used in the older script.
    """
    edges, centers = _make_bin_edges(x_start, x_stop, bin_size_s=bin_size_s)

    if not spike_train_list:
        return centers, np.zeros((0, len(centers)), dtype=float)

    rows = []
    for st in spike_train_list:
        st = np.asarray(st, dtype=float).reshape(-1)
        st = st[np.isfinite(st)]
        counts, _ = np.histogram(st, bins=edges)
        rows.append(counts.astype(float) / max(1e-12, bin_size_s))

    rates = np.vstack(rows) if rows else np.zeros((0, len(centers)), dtype=float)

    sigma_bins = float(smooth_sigma_s) / max(1e-12, float(bin_size_s))
    if rates.size and sigma_bins > 0:
        rates = gaussian_filter1d(rates, sigma=sigma_bins, axis=1, mode="nearest")

    return centers, rates


def _safe_sem(a, axis=0):
    a = np.asarray(a, dtype=float)
    if a.size == 0:
        return a
    try:
        return stats.sem(a, axis=axis, nan_policy="omit")
    except TypeError:
        return stats.sem(a, axis=axis)


def _plot_rates_and_histograms(ax_rate, spike_train_list, x_start, x_stop):
    if not spike_train_list:
        ax_rate.text(0.5, 0.5, "No events / no spike trains", ha="center", va="center", transform=ax_rate.transAxes)
        ax_rate.set_xlim(x_start, x_stop)
        return

    centers, rates = _rate_matrix_from_spike_trains(
        spike_train_list,
        x_start=x_start,
        x_stop=x_stop,
        bin_size_s=0.05,
        smooth_sigma_s=0.10,
    )

    ax_rate.set_ylabel("Frequency (Hz)")
    ax_rate.set_xlim(x_start, x_stop)

    if rates.size == 0 or rates.shape[1] == 0:
        ax_rate.text(0.5, 0.5, "No rate data", ha="center", va="center", transform=ax_rate.transAxes)
        return

    # Mean PSTH / smoothed firing rate.
    mean_rate = np.nanmean(rates, axis=0)
    ax_rate.plot(centers, mean_rate)
    if rates.shape[0] > 1:
        sem_rate = _safe_sem(rates, axis=0)
        ax_rate.fill_between(centers, mean_rate - sem_rate, mean_rate + sem_rate, alpha=0.35)
 
def _plot_raster(ax, spike_train_list, x_start, x_stop):
    """
    Replacement for external raster package.rasterplot.rasterplot_rates.

    Draws one raster row per event/trial and overlays a mean firing-rate curve
    on a right-side y-axis.
    """
    ax.set_xlim(x_start, x_stop)
    ax.set_ylabel("Trial / event")

    if not spike_train_list:
        ax.text(0.5, 0.5, "No events / no spike trains", ha="center", va="center", transform=ax.transAxes)
        return

    # Raster rows.
    # Draw real dots instead of very thin vlines. This keeps dense rasters
    # readable in the saved PNG/PDF and makes single spikes easier to see.
    x_values = []
    y_values = []
    for i, st in enumerate(spike_train_list):
        st = np.asarray(st, dtype=float).reshape(-1)
        st = st[np.isfinite(st)]
        if st.size:
            x_values.append(st)
            y_values.append(np.full(st.shape, i + 1.0, dtype=float))

    if x_values:
        x_all = np.concatenate(x_values)
        y_all = np.concatenate(y_values)
        n_spikes = int(x_all.size)

        # Keep spikes visible in the saved PNG viewer. Very small ``.`` markers
        # can disappear when the summary image is scaled down inside Qt, so use
        # small filled circles with an adaptive size.
        if n_spikes >= 20000:
            dot_size = 2.0
            dot_alpha = 0.62
        elif n_spikes >= 10000:
            dot_size = 2.4
            dot_alpha = 0.68
        elif n_spikes >= 5000:
            dot_size = 2.8
            dot_alpha = 0.74
        elif n_spikes >= 2000:
            dot_size = 3.3
            dot_alpha = 0.80
        else:
            dot_size = 4.0
            dot_alpha = 0.85

        ax.scatter(
            x_all,
            y_all,
            s=dot_size,
            marker="o",
            linewidths=0,
            edgecolors="none",
            alpha=dot_alpha,
            color="black",
            rasterized=True,
            zorder=3,
        )

    ax.set_ylim(0.5, len(spike_train_list) + 0.5)

    # Overlay mean rate on twin axis.
    try:
        centers, rates = _rate_matrix_from_spike_trains(
            spike_train_list,
            x_start=x_start,
            x_stop=x_stop,
            bin_size_s=0.05,
            smooth_sigma_s=0.10,
        )

    except Exception:
        pass


# -----------------------------------------------------------------------------
# Plot helpers
# -----------------------------------------------------------------------------

def get_highest_amplitude_channel(waveforms):
    waveforms = np.asarray(waveforms)
    if waveforms.ndim != 3 or waveforms.shape[0] == 0:
        return np.array([])

    selected = []
    for i in range(waveforms.shape[0]):
        wf = waveforms[i, :, :]
        # Largest absolute negative deflection, matching your original behavior.
        ch_idx = int(np.argmin(wf.min(axis=0)))
        selected.append(wf[:, ch_idx])
    return np.asarray(selected)



def _unique_relative_spans(spans, decimals=6):
    """Return one span for each unique relative start/stop pair.

    Overlay event maps usually contain one absolute event per trial. When all
    trials have the same relative timing, drawing every copy saturates the
    color. This helper keeps only one plotted span per relative position.
    """
    try:
        arr = np.asarray(spans, dtype=float).reshape(-1, 2)
    except Exception:
        return []

    if arr.size == 0:
        return []

    arr = arr[np.isfinite(arr).all(axis=1)]
    if arr.size == 0:
        return []

    arr = arr[arr[:, 1] > arr[:, 0]]
    if arr.size == 0:
        return []

    rounded = np.round(arr, int(decimals))
    out = []
    seen = set()
    for start, stop in rounded:
        key = (float(start), float(stop))
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _unique_relative_times(times, decimals=6):
    """Return one time for each unique relative event position."""
    try:
        arr = np.asarray(times, dtype=float).reshape(-1)
    except Exception:
        return []

    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return []

    rounded = np.round(arr, int(decimals))
    out = []
    seen = set()
    for value in rounded:
        key = float(value)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out

def add_event(ax, event_dict):
    """Draw event overlays on an axis.

    Backward compatible forms:
      event_time: scalar -> axvline
      event_time: [start, stop] -> axvspan

    New forms used by the GUI unit-summary overlay:
      event_times: [t0, t1, ...] -> multiple axvline
      event_spans: [[start, stop], ...] -> multiple axvspan
    """
    if event_dict is None:
        return

    for event_name in event_dict.keys():
        ev = event_dict[event_name] or {}
        color = ev.get("color", None)

        spans = ev.get("event_spans", None)
        if spans is not None:
            try:
                spans = np.asarray(spans, dtype=float).reshape(-1, 2)
            except Exception:
                spans = np.empty((0, 2), dtype=float)
            alpha = ev.get("alpha", 0.18)
            spans = _unique_relative_spans(spans)
            for start, stop in spans:
                ax.axvspan(float(start), float(stop), color=color, alpha=alpha, zorder=0)

        times = ev.get("event_times", None)
        if times is not None:
            try:
                times = np.asarray(times, dtype=float).reshape(-1)
            except Exception:
                times = np.array([], dtype=float)
            alpha = ev.get("alpha", 0.75)
            times = _unique_relative_times(times)
            for event_time in times:
                ax.axvline(float(event_time), color=color, alpha=alpha, zorder=1)

        event_time = ev.get("event_time", None)
        if event_time is None:
            continue
        alpha = ev.get("alpha", 0.75)
        if isinstance(event_time, (list, tuple, np.ndarray)):
            try:
                values = np.asarray(event_time, dtype=float).reshape(-1)
            except Exception:
                continue
            if len(values) == 2:
                ax.axvspan(float(values[0]), float(values[1]), color=color, alpha=ev.get("alpha", 0.18), zorder=0)
            else:
                for one_time in _unique_relative_times(values):
                    ax.axvline(float(one_time), color=color, alpha=alpha, zorder=1)
        else:
            try:
                event_time = float(event_time)
            except Exception:
                continue
            if np.isfinite(event_time):
                ax.axvline(event_time, color=color, alpha=alpha, zorder=1)


def _relative_plot_event_overlays(plot_events, alignment_event_frames, x_start, x_stop, sampling_frequency):
    """Convert absolute sorter-frame event overlays to relative plot coordinates.

    The unit-summary raster/PSTH is aligned to each entry of alignment_event_frames.
    Event overlays are stored in sorter frames, so this function converts them into
    relative seconds within the plotted alignment window. Interval events become
    axvspan-compatible spans. Repeated relative positions are collapsed so an
    event with identical timing trial after trial is drawn once, not hundreds
    of times on top of itself.
    """
    if not plot_events:
        return {}
    if alignment_event_frames is None:
        return {}

    align_frames = np.asarray(alignment_event_frames, dtype=float).reshape(-1)
    align_frames = align_frames[np.isfinite(align_frames)]
    if align_frames.size == 0:
        return {}

    sf = float(sampling_frequency)
    x_start = float(x_start)
    x_stop = float(x_stop)
    if not np.isfinite(sf) or sf <= 0 or x_stop <= x_start:
        return {}

    overlay_dict = {}
    for idx, event_cfg in enumerate(plot_events, start=1):
        if not isinstance(event_cfg, dict):
            continue
        label = str(event_cfg.get("label", event_cfg.get("event_marker", f"event_{idx}")) or f"event_{idx}")
        color = event_cfg.get("color", None)
        display = str(event_cfg.get("display", "duration") or "duration").lower()

        if display == "duration":
            intervals = event_cfg.get("event_intervals", None)
            if intervals is None:
                intervals = event_cfg.get("interval_frames", None)
            try:
                intervals = np.asarray(intervals, dtype=float).reshape(-1, 2)
            except Exception:
                intervals = np.empty((0, 2), dtype=float)
            if intervals.size == 0:
                continue
            intervals = intervals[np.isfinite(intervals).all(axis=1)]
            if intervals.size == 0:
                continue
            intervals = intervals[intervals[:, 1] > intervals[:, 0]]
            if intervals.size == 0:
                continue

            rel_spans = []
            for ev_frame in align_frames:
                win_start = ev_frame + x_start * sf
                win_stop = ev_frame + x_stop * sf
                mask = (intervals[:, 1] >= win_start) & (intervals[:, 0] <= win_stop)
                if not np.any(mask):
                    continue
                for start_frame, stop_frame in intervals[mask]:
                    rel_start = (start_frame - ev_frame) / sf
                    rel_stop = (stop_frame - ev_frame) / sf
                    rel_start = max(x_start, float(rel_start))
                    rel_stop = min(x_stop, float(rel_stop))
                    if rel_stop > rel_start:
                        rel_spans.append((rel_start, rel_stop))
            rel_spans = _unique_relative_spans(rel_spans)
            if rel_spans:
                overlay_dict[label] = {
                    "event_spans": rel_spans,
                    "color": color,
                    "alpha": float(event_cfg.get("alpha", 0.18)),
                }
        else:
            frames = event_cfg.get("event_frames", None)
            try:
                frames = np.asarray(frames, dtype=float).reshape(-1)
            except Exception:
                frames = np.array([], dtype=float)
            frames = frames[np.isfinite(frames)]
            if frames.size == 0:
                continue
            rel_times = []
            for ev_frame in align_frames:
                win_start = ev_frame + x_start * sf
                win_stop = ev_frame + x_stop * sf
                mask = (frames >= win_start) & (frames <= win_stop)
                if not np.any(mask):
                    continue
                rel_times.extend(((frames[mask] - ev_frame) / sf).tolist())
            rel_times = _unique_relative_times(rel_times)
            if rel_times:
                overlay_dict[label] = {
                    "event_times": rel_times,
                    "color": color,
                    "alpha": float(event_cfg.get("alpha", 0.75)),
                }

    return overlay_dict


# -----------------------------------------------------------------------------
# Main plotting functions
# -----------------------------------------------------------------------------

def plot_summary_for_unit(
    unit_id,
    analyzer,
    sorter_name,
    trial_len=None,
    fig=None,
    ylim=None,
    save_path=None,
    unit_for_plot_name=None,
    event_dict=None,
    event_frames=None,
    pad_before_s=0.0,
    pad_after_s=0.0,
    event_label=None,
    plot_events=None,
    job_kwargs=None,
    n_jobs=None,
):
    """
    Plot the unit summary.

    If event_frames is provided, raster/PSTH/rate panels are centered on each
    event frame. Otherwise the old fixed trial_len splitting is used.
    """
    event_aligned = event_frames is not None

    if event_aligned:
        before = max(0.0, float(pad_before_s))
        after = max(0.0, float(pad_after_s))
        if before <= 0 and after <= 0:
            after = 1.0

        spike_train_list = make_event_aligned_spike_trains(
            analyzer=analyzer,
            unit_id=unit_id,
            event_frames=event_frames,
            pad_before_s=before,
            pad_after_s=after,
        )
        x_start = -before
        x_stop = after
        event_label = str(event_label or "event")
        rate_title = f"Mean firing rate aligned to {event_label}"
        raster_title = f"Raster aligned to {event_label}"
        if event_dict is None:
            event_dict = {event_label: {"event_time": 0.0, "color": "red", "alpha": 0.8}}

        extra_events = _relative_plot_event_overlays(
            plot_events=plot_events,
            alignment_event_frames=event_frames,
            x_start=x_start,
            x_stop=x_stop,
            sampling_frequency=_get_sampling_frequency(analyzer),
        )
        if extra_events:
            event_dict = dict(event_dict or {})
            event_dict.update(extra_events)
    else:
        if trial_len is None:
            try:
                trial_len = float(analyzer.recording.get_duration())
            except Exception:
                trial_len = 1.0
        spike_train_list = make_fixed_length_spike_trains(analyzer, unit_id, trial_len=trial_len)
        x_start = 0.0
        x_stop = float(trial_len)
        rate_title = "Mean firing rate during a trial"
        raster_title = "Raster during a trial"

    unit_for_plot_name = unit_id if unit_for_plot_name is None else unit_for_plot_name

    if fig is None:
        fig = plt.figure(figsize=(30, 13))

    gs = GridSpec(nrows=3, ncols=6)
    fig.suptitle(f"{sorter_name}\nunit {unit_for_plot_name} (Total spikes {_safe_count_spikes(analyzer, unit_id)})")

    # Layout chosen for the GUI summary browser. Keep this order/positioning
    # when updating this file.
    ax0 = fig.add_subplot(gs[0, 0:2])

    ax2 = fig.add_subplot(gs[0, 2:5])
    ax2.set_title("Waveform of the unit")

    ax5 = fig.add_subplot(gs[0, 5])

    ax7 = fig.add_subplot(gs[1:3, 0:3])

    ax1 = fig.add_subplot(gs[1, 3:6])
    ax1.set_title(rate_title)
    add_event(ax1, event_dict)

    ax6 = fig.add_subplot(gs[2, 3:6])
    ax6.set_title(raster_title)
    add_event(ax6, event_dict)

    window_ms_autocorr = 200
    try:
        sw.plot_autocorrelograms(
            analyzer.sorting,
            unit_ids=[unit_id],
            axes=ax0,
            bin_ms=1,
            window_ms=window_ms_autocorr,
        )
        ax0.set_xlim(-(window_ms_autocorr / 2), (window_ms_autocorr / 2))
    except Exception as exc:
        ax0.text(0.5, 0.5, f"Autocorrelogram failed:\n{exc}", ha="center", va="center", transform=ax0.transAxes)
    ax0.set_title("Autocorrelogram")

    _plot_rates_and_histograms(ax1, spike_train_list, x_start, x_stop)

    try:
        sw.plot_spike_locations(analyzer, unit_ids=[unit_id], ax=ax7, with_channel_ids=True)
    except Exception as exc:
        ax7.text(0.5, 0.5, f"Spike locations failed:\n{exc}", ha="center", va="center", transform=ax7.transAxes)
    ax7.set_title("Unit localisation")

    try:
        sw.plot_unit_waveforms_density_map(analyzer, unit_ids=[unit_id], ax=ax2)
    except Exception as exc:
        ax2.text(0.5, 0.5, f"Waveform density failed:\n{exc}", ha="center", va="center", transform=ax2.transAxes)
    if ylim is not None:
        ax2.set_ylim(ylim)

    try:
        wf_all = analyzer.get_extension("waveforms").get_waveforms_one_unit(unit_id=unit_id, force_dense=True)
        wf_max = get_highest_amplitude_channel(wf_all).T
        if wf_max.size > 0:
            ax5.plot(wf_max, alpha=0.1)
            ax5.plot(np.median(wf_max, axis=1))
    except Exception as exc:
        ax5.text(0.5, 0.5, f"Max-channel waveforms failed:\n{exc}", ha="center", va="center", transform=ax5.transAxes)
    ax5.set_title("Waveform of all spikes\n(at max channel)")

    try:
        _plot_raster(ax6, spike_train_list, x_start, x_stop)
    except Exception as exc:
        ax6.text(0.5, 0.5, f"Raster failed:\n{exc}", ha="center", va="center", transform=ax6.transAxes)
        ax6.set_xlim(x_start, x_stop)

    if event_aligned:
        for ax in (ax1, ax6):
            ax.set_xlabel("Time from event (s)")
    else:
        for ax in (ax1, ax6):
            ax.set_xlabel("Time (s)")

    plt.tight_layout()

    if save_path is not None:
        save_path = Path(save_path)
        save_path.mkdir(parents=True, exist_ok=True)
        png_dir = save_path / "png_version"
        png_dir.mkdir(parents=True, exist_ok=True)

        try:
            safe_unit = int(unit_for_plot_name)
        except Exception:
            safe_unit = str(unit_for_plot_name).replace(os.sep, "_")

        fig.savefig(save_path / f"Unit_{safe_unit}.pdf")
        fig.savefig(png_dir / f"Unit_{safe_unit}.png", dpi=180)
        plt.close(fig)

    return fig


def parallel_plot_summary(args):
    return plot_summary_for_unit(*args)


def plot_sorting_summary(
    analyzer,
    sorter_name,
    trial_len=None,
    save_path=None,
    save_folder=None,
    acelerate=False,
    event_dict=None,
    event_frames=None,
    pad_before_s=0.0,
    pad_after_s=0.0,
    event_label=None,
    plot_events=None,
    job_kwargs=None,
    n_jobs=None,
):
    """
    Plot one summary figure per unit.

    Backward compatible with the old signature using trial_len/save_path. New
    event-centered mode is enabled by passing event_frames + pad_before_s/
    pad_after_s. event_frames must be in sorter/analyzer frame coordinates.
    """
    if save_path is None and save_folder is not None:
        save_path = save_folder

    job_kwargs = dict(job_kwargs or {})
    if n_jobs is not None and "n_jobs" not in job_kwargs:
        try:
            job_kwargs["n_jobs"] = max(1, int(n_jobs))
        except Exception:
            pass

    load_or_compute_extension(analyzer, ["random_spikes", "waveforms", "templates", "spike_locations"], job_kwargs=job_kwargs)

    max_unit_amplitude = 0.0
    min_unit_amplitude = 0.0
    try:
        for _, templ in get_template_amplitudes(analyzer, peak_sign="pos").items():
            templ = np.asarray(templ, dtype=float)
            if templ.size:
                max_unit_amplitude = max(max_unit_amplitude, float(np.nanmax(templ)))
        for _, templ in get_template_amplitudes(analyzer, peak_sign="neg").items():
            templ = np.asarray(templ, dtype=float)
            if templ.size:
                min_unit_amplitude = min(min_unit_amplitude, float(-1 * np.nanmax(templ)))
    except Exception:
        max_unit_amplitude = 0.0
        min_unit_amplitude = 0.0

    ylim = None
    if np.isfinite(max_unit_amplitude) and np.isfinite(min_unit_amplitude) and max_unit_amplitude != min_unit_amplitude:
        ylim_margin = (max_unit_amplitude - min_unit_amplitude) * 0.01
        ylim = (min_unit_amplitude - ylim_margin, max_unit_amplitude + ylim_margin)

    # Multiprocessing with SortingAnalyzer copies can be fragile, so keep it opt-in.
    if acelerate:
        args = [
            (
                unit_id,
                analyzer.copy(),
                sorter_name,
                trial_len,
                None,
                ylim,
                save_path,
                None,
                event_dict,
                event_frames,
                pad_before_s,
                pad_after_s,
                event_label,
                plot_events,
                job_kwargs,
                n_jobs,
            )
            for unit_id in analyzer.unit_ids
        ]
        try:
            workers = int(job_kwargs.get("n_jobs", 0) or 0)
        except Exception:
            workers = 0
        if workers <= 0:
            workers = max(1, (os.cpu_count() or 1) - 1)
        try:
            with mp.Pool(processes=max(1, workers)) as pool:
                pool.map(parallel_plot_summary, args)
            return
        except TypeError as exc:
            if "pickle" not in str(exc).lower() and "weakref" not in str(exc).lower():
                raise
            print(
                "Multiprocessing unit-summary generation is not possible for this analyzer "
                f"({exc}). Falling back to sequential generation."
            )
        except Exception as exc:
            if "pickle" not in str(exc).lower() and "weakref" not in str(exc).lower():
                raise
            print(
                "Multiprocessing unit-summary generation is not possible for this analyzer "
                f"({exc}). Falling back to sequential generation."
            )

    for unit_id in tqdm(analyzer.unit_ids, desc="Plot sorting summary"):
        plot_summary_for_unit(
            unit_id=unit_id,
            analyzer=analyzer,
            sorter_name=sorter_name,
            trial_len=trial_len,
            ylim=ylim,
            unit_for_plot_name=None,
            save_path=save_path,
            event_dict=event_dict,
            event_frames=event_frames,
            pad_before_s=pad_before_s,
            pad_after_s=pad_after_s,
            event_label=event_label,
            plot_events=plot_events,
            job_kwargs=job_kwargs,
            n_jobs=n_jobs,
        )
