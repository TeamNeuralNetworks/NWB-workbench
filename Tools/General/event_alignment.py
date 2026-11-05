"""Independent event boundaries, without resampling or interpolation."""
import json
import numpy as np
import pandas as pd
import xarray as xr

ALIGNMENT_VERSION = 2


def normalize_alignment(value):
    a = dict(value or {})
    a['start_offset_s'] = float(a['start_offset_s'] if 'start_offset_s' in a else -float(a.get('pad_before_s', 0)))
    a['end_offset_s'] = float(a['end_offset_s'] if 'end_offset_s' in a else a.get('pad_after_s', 0))
    a.setdefault('edge', 'start')
    a.setdefault('end_event_marker', a.get('event_marker'))
    a.setdefault('end_event_path', a.get('event_path') if a['end_event_marker'] == a.get('event_marker') else None)
    a.setdefault('end_edge', a['edge'])
    if not np.isfinite([a['start_offset_s'], a['end_offset_s']]).all():
        raise ValueError('Alignment offsets must be finite numbers.')
    if a['edge'] not in ('start', 'stop') or a['end_edge'] not in ('start', 'stop'):
        raise ValueError('Alignment edges must be start or stop.')
    if same_reference(a) and a['end_offset_s'] <= a['start_offset_s']:
        raise ValueError('For the same event and edge, the end offset must be later than the start offset.')
    return a


def same_marker(a):
    return (a.get('event_path') or a.get('event_marker')) == (a.get('end_event_path') or a.get('end_event_marker'))


def same_reference(a):
    return same_marker(a) and a['edge'] == a['end_edge']


def alignment_signature(value):
    a = normalize_alignment(value)
    keys = ('event_marker', 'event_path', 'edge', 'end_event_marker', 'end_event_path',
            'end_edge', 'start_offset_s', 'end_offset_s')
    return json.dumps({'version': ALIGNMENT_VERSION, **{k: a.get(k) for k in keys}}, sort_keys=True)


def event_rows(marker, edge):
    """Return full event sequence; retain interval row IDs for trial selection."""
    if marker is None:
        return pd.DataFrame({'reference_time': pd.Series(dtype=float), 'event_number': pd.Series(dtype=int)})
    for field in ('time_series', 'interval_series'):
        children = getattr(marker, field, None)
        if children is not None:
            if len(children) != 1:
                raise ValueError('Event container has several series. Select a single event series.')
            marker = next(iter(children.values()))
            break
    if hasattr(marker, 'to_dataframe'):
        df = marker.to_dataframe().copy()
        times = np.asarray(df['start_time' if edge == 'start' else 'stop_time'], float)
        df['event_length'] = df['stop_time'] - df['start_time']
    else:
        ts = getattr(marker, 'timestamps', None)
        if ts is None:
            ts = getattr(marker, 'times', None)
        if ts is None:
            raise ValueError('Selected event marker has no supported event timestamps.')
        times = np.asarray(ts[:], float)
        if type(marker).__name__ == 'IntervalSeries':
            codes = np.asarray(marker.data[:])
            opening = np.flatnonzero(codes > 0)
            closing = np.flatnonzero(codes < 0)
            stops = []
            for i, position in enumerate(opening):
                limit = opening[i + 1] if i + 1 < len(opening) else len(times)
                candidates = closing[(closing > position) & (closing < limit)]
                stops.append(times[candidates[0]] if len(candidates) else np.nan)
            df = pd.DataFrame({'start_time': times[opening], 'stop_time': stops})
            df['event_length'] = df.stop_time - df.start_time
            times = df['start_time' if edge == 'start' else 'stop_time'].to_numpy(float)
        else:
            df = pd.DataFrame(index=np.arange(len(times)))
    df['reference_time'] = times
    df['event_number'] = np.arange(len(df)) + 1
    return df


def pair_segments(start_marker, end_marker, alignment, trials=None, selected_trials=None):
    """Pair full sequences first, then filter by the trial containing the start.

    Same marker uses the same occurrence (including start/stop of an interval).
    Distinct markers use the first end at/after the start, before the next start.
    """
    a = normalize_alignment(alignment)
    starts = event_rows(start_marker, a['edge'])
    ends = event_rows(end_marker, a['end_edge'])
    ordered = starts[np.isfinite(starts.reference_time)].sort_values('reference_time', kind='stable')
    valid_ends = ends[np.isfinite(ends.reference_time)].sort_values('reference_time', kind='stable')
    end_times = valid_ends.reference_time.to_numpy(float)
    records = []
    skipped = {'missing_end': 0, 'invalid_window': 0, 'filtered': 0}
    for pos, (row_id, row) in enumerate(ordered.iterrows()):
        t = float(row.reference_time)
        next_start = float(ordered.iloc[pos + 1].reference_time) if pos + 1 < len(ordered) else np.inf
        trial_id = None
        trial_stop = np.inf
        if trials is not None:
            if a['event_marker'] == 'trials' and row_id in trials.index:
                trial_id = row_id
            else:
                hits = trials[(trials.start_time <= t) & (t < trials.stop_time)]
                if not hits.empty:
                    trial_id = hits.index[0]
            if trial_id is not None:
                trial_stop = float(trials.loc[trial_id, 'stop_time'])
        if same_marker(a):
            match = ends[ends.event_number == row.event_number]
            end_time = float(match.iloc[0].reference_time) if len(match) else np.nan
        else:
            idx = np.searchsorted(end_times, t, side='left')
            end_time = float(end_times[idx]) if idx < len(end_times) else np.nan
            if end_time >= next_start or end_time > trial_stop:
                end_time = np.nan
        if not np.isfinite(end_time) or end_time < t:
            skipped['missing_end'] += 1
            continue
        begin, finish = t + a['start_offset_s'], end_time + a['end_offset_s']
        if finish <= begin:
            skipped['invalid_window'] += 1
            continue
        if selected_trials is not None:
            if a['event_marker'] == 'trials':
                selected = row_id in selected_trials.index
            elif trials is not None:
                selected = trial_id in selected_trials.index
            else:
                selected = bool(((selected_trials.start_time <= t) & (t < selected_trials.stop_time)).any())
            if not selected:
                skipped['filtered'] += 1
                continue
        rec = row.to_dict()
        rec.update(segment_start_time=begin, segment_stop_time=finish,
                   end_event_time=end_time, segment_duration_s=finish-begin,
                   end_event_relative_s=end_time-t, segment_stop_relative_s=finish-t,
                   source_event_index=int(row.event_number)-1)
        if trial_id is not None:
            rec['source_trial_id'] = trial_id
        records.append(rec)
    print(f"Event alignment: {len(records)} kept; {skipped['missing_end']} missing end; "
          f"{skipped['invalid_window']} invalid window; {skipped['filtered']} excluded by trial filter.")
    return pd.DataFrame(records), a


def regular_sample_interval(data):
    time = np.asarray(data.time.values, float)
    if len(time) < 2 or not np.isfinite(time).all():
        raise ValueError('Alignment needs at least two finite signal timestamps.')
    differences = np.diff(time)
    rate = data.attrs.get('rate')
    dt = 1 / float(rate) if rate is not None else float(np.median(differences))
    if not np.isfinite(dt) or dt <= 0 or not np.allclose(differences, dt, rtol=1e-7, atol=1e-9):
        raise ValueError('Signal timestamps are not regularly sampled. No resampling was performed.')
    return dt


def validate_sampling_rates(arrays):
    """Check rates, then unify floating-point time labels without changing values."""
    rates = [float(v.attrs['alignment_sample_interval_s']) for v in arrays
             if 'alignment_sample_interval_s' in v.attrs]
    if rates and not np.allclose(rates, rates[0], rtol=1e-9, atol=1e-12):
        raise ValueError('Cannot combine signals with different sampling rates. No resampling was performed. '
                         'Select recordings with the same sampling rate.')
    if not rates:
        return arrays
    result = []
    for data in arrays:
        if 'alignment_sample_interval_s' in data.attrs:
            origin = float(data.attrs['start_offset_s'])
            grid = np.round(origin + np.arange(data.sizes['time'])*rates[0], 12)
            if len(np.unique(grid)) != len(grid):
                raise ValueError('Sampling interval is too small for the alignment time precision.')
            data = data.assign_coords(time=grid)
        result.append(data)
    return result


def align_signal(data, segments, alignment, identifier, save_metadata=True):
    if segments.empty:
        return None
    a = normalize_alignment(alignment)
    dt = regular_sample_interval(data)
    start = a['start_offset_s']
    end = float(segments.segment_stop_relative_s.max())
    # Half-open windows [start, end). No interpolation: copy each source sample
    # into its nearest event-relative bin, with at most half a sample uncertainty.
    count = int(np.ceil((end-start)/dt - 1e-9))
    grid = np.round(start + np.arange(count)*dt, 12)
    signal = data.transpose('time', *[d for d in data.dims if d != 'time'])
    values = np.asarray(signal.values)
    out = np.full((count, *values.shape[1:], len(segments)), np.nan,
                  dtype=np.result_type(values.dtype, np.float64))
    timestamps = np.asarray(signal.time.values, float)
    valid_counts = []
    grid_offsets = []
    for col, row in enumerate(segments.itertuples()):
        lo = np.searchsorted(timestamps, row.segment_start_time, side='left')
        hi = np.searchsorted(timestamps, row.segment_stop_time, side='left')
        bins = np.rint((timestamps[lo:hi] - row.reference_time - start)/dt).astype(int)
        good = (bins >= 0) & (bins < count)
        good &= start + bins*dt < row.segment_stop_relative_s - dt*1e-9
        out[bins[good], ..., col] = values[lo:hi][good]
        valid_counts.append(int(good.sum()))
        delta = timestamps[lo:hi][good] - row.reference_time - grid[bins[good]]
        grid_offsets.append(float(np.median(delta)) if len(delta) else np.nan)
    dim = a['event_marker']
    coords = {k: v for k, v in signal.coords.items() if 'time' not in v.dims}
    coords.update(time=grid)
    coords[dim] = [f'{identifier}_{int(n)}' for n in segments.event_number]
    metadata = ['reference_time', 'end_event_time', 'segment_start_time', 'segment_stop_time',
                'segment_duration_s', 'end_event_relative_s', 'segment_stop_relative_s']
    if save_metadata:
        metadata = list(segments.columns)
    for name in metadata:
        if name not in coords and name not in signal.dims:
            coords[name] = (dim, segments[name].to_numpy())
    coords['valid_sample_count'] = (dim, valid_counts)
    coords['sample_grid_offset_s'] = (dim, grid_offsets)
    attrs = dict(signal.attrs)
    attrs.update(aligned_to=dim, alignent_edge=a['edge'], end_event_marker=a['end_event_marker'],
                 end_edge=a['end_edge'], start_offset_s=start, end_offset_s=a['end_offset_s'],
                 pad_before_s=start, pad_after_s=a['end_offset_s'],
                 alignment_sample_interval_s=dt, rate=1/dt, alignment_version=ALIGNMENT_VERSION,
                 alignment_signature=alignment_signature(a))
    return xr.DataArray(out, dims=(*signal.dims, dim), coords=coords, attrs=attrs)
