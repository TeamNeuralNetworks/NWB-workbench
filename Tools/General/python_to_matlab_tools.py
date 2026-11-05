# -*- coding: utf-8 -*-
"""
Created on Sun Mar  1 15:04:53 2026

@author: Pierre
"""

import importlib.util

module_name = "matlab"

if importlib.util.find_spec(module_name) is not None:
    
    import matlab.engine
    
    # warnings.filterwarnings(
    #     "ignore",
    #     message="Ignoring cached namespace 'core' version",
    #     category=UserWarning,
    #     module="hdmf.spec.namespace"
    # )
    
    SHARED_NAME = 'myPoolMATLAB'           # keep the name in one place
    
    def get_or_start_engine(workers_target, matlab_path):
        """
        Attach to an existing shared MATLAB with a ready pool, or
        start a new engine and open the pool once.
        Returns (eng, workers_in_pool).
        """
    
        # ── 1. Try to hook onto the shared MATLAB ──────────────────────────────
        names = matlab.engine.find_matlab()     # ✱ cheap call ✱ :contentReference[oaicite:0]{index=0}
        if SHARED_NAME in names:
            eng = matlab.engine.connect_matlab(SHARED_NAME)  # instant attach :contentReference[oaicite:1]{index=1}
            return eng, int(eng.eval("gcp().NumWorkers", nargout=1))
    
        # ── 2. Start a new engine and open the pool once ───────────────────────
        eng = matlab.engine.start_matlab()
        eng.addpath(matlab_path, nargout=0)
    
        # open the pool once for this engine; use threads if the code is thread‑safe
        eng.eval(f"if isempty(gcp('nocreate')); "
                 f"parpool('local',{workers_target}); "
                 f"end", nargout=0)
    
        return eng, workers_target
    
    def numpy_arrays_to_matlab_cell(np_arrays):
        np_arrays = [np_arrays[:, i] for i in range(np_arrays.shape[1])]
        return [matlab.double(arr.tolist()) for arr in np_arrays]
