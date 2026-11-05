# -*- coding: utf-8 -*-
"""
Created on Sat Mar 21 10:19:11 2026

@author: Pierre
"""

import numpy as np

def detect_sampling_rate_and_gaps_in_timestamps(
    timestamp,
    gap_factor=2,
    validate=True,
    min_segment_duration=0.5,   # seconds
):
    """
    Detect sampling rate and gaps in timestamp.

    Parameters
    ----------
    timestamp : array-like
    gap_factor : float
        Threshold factor to detect gaps (multiple of median dt)
    validate : bool
        If True, raise error when segmentation is not suitable
    min_segment_duration : float
        Minimum duration (in seconds) required per segment
    max_gap_density : float or None
        Maximum allowed gaps per second

    Returns
    -------
    sampling_rate : float
    gaps : list of (start, stop)
    """
    timestamp = np.asarray(timestamp)

    d = np.diff(timestamp)

    if np.any(d <= 0):
        raise ValueError("Timestamps must be strictly increasing")

    dt = np.median(d)
    sampling_rate = 1.0 / dt

    breaks = np.where(d > gap_factor * dt)[0] + 1

    starts = np.r_[0, breaks]
    stops = np.r_[breaks, len(timestamp)]

    gaps = list(zip(starts, stops))

    # ---------------- VALIDATION ----------------
    if validate:

        # 1. minimum segment length
        min_samples = int(min_segment_duration * sampling_rate)

        for start, stop in gaps:
            if (stop - start) < min_samples:
                raise ValueError(
                    f"Segment too short: {(stop - start)} samples "
                    f"(min required: {min_samples})"
                )


    return sampling_rate, gaps

def check_resampling_consistency(old_t, new_t, old_fs, new_fs):
    old_duration = old_t[-1] - old_t[0]
    new_duration = new_t[-1] - new_t[0]

    # tolerance = one sample of the coarser grid
    tol = max(1/old_fs, 1/new_fs)

    if abs(old_duration - new_duration) > tol:
        raise ValueError(
            f"Duration mismatch after resampling: "
            f"{old_duration:.6f} vs {new_duration:.6f} (tol={tol:.6f})"
        )

    dt_new = np.diff(new_t)

    # tolerance for spacing (smaller, since internal consistency should be tighter)
    dt_tol = tol * 0.1

    if not np.allclose(dt_new, 1/new_fs, atol=dt_tol):
        raise ValueError("New timestamps are not uniformly spaced")
