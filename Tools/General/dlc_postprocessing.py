"""Deterministic, timestamp-aware DLC postprocessing; raw values are never modified."""
import hashlib
import json
import numpy as np


def normalize_settings(settings):
    settings=settings or {}
    result=dict(enabled=bool(settings.get('enabled',False)),version=1,
                confidence=float(settings.get('confidence',.75)),
                anchor_confidence=float(settings.get('anchor_confidence',.9)),
                max_gap_seconds=float(settings.get('max_gap_seconds',.2)),
                fixed_min_samples=int(settings.get('fixed_min_samples',10)),
                fixed_estimator=str(settings.get('fixed_estimator','median')),
                modes=dict(settings.get('modes',{})))
    if not 0<=result['confidence']<=result['anchor_confidence']<=1:
        raise ValueError('Surrounding-point confidence must be at least the minimum confidence (both between 0 and 1).')
    if not np.isfinite(result['max_gap_seconds']) or result['max_gap_seconds']<=0:
        raise ValueError('Maximum gap must be positive.')
    if result['fixed_min_samples']<1 or result['fixed_estimator'] not in {'median','mean'}:
        raise ValueError('Invalid fixed-point settings.')
    if any(mode not in {'raw','moving','fixed'} for mode in result['modes'].values()):
        raise ValueError('Unknown body-part processing mode.')
    return result


def settings_hash(settings):
    return hashlib.sha256(json.dumps(normalize_settings(settings),sort_keys=True,separators=(',',':')).encode()).hexdigest()[:12]


def process_pose(values,parts,times,settings):
    """Status codes: 0 missing, 1 original, 2 interpolated, 3 fixed estimate.

    Max gap is the elapsed time between the two bracketing measured frames.
    Fixed estimates are calculated independently for this video, per coordinate.
    """
    cfg=normalize_settings(settings);values=np.asarray(values);times=np.asarray(times,dtype=float)
    if values.shape!=(len(times),len(parts),3) or not len(times) or not np.all(np.isfinite(times)) or np.any(np.diff(times)<=0):
        raise ValueError('Postprocessing needs one increasing timestamp per prediction frame.')
    xy=values[:,:,:2].astype(float,copy=True)
    status=np.where(np.all(np.isfinite(xy),axis=2),1,0).astype(np.uint8)
    warnings=[]
    for i,part in enumerate(parts):
        mode=cfg['modes'].get(part,'raw') if cfg['enabled'] else 'raw'
        if mode=='raw':continue
        valid=np.all(np.isfinite(xy[:,i]),axis=1)&np.isfinite(values[:,i,2])&(values[:,i,2]>=cfg['confidence'])
        xy[~valid,i]=np.nan;status[~valid,i]=0
        if mode=='fixed':
            count=int(valid.sum())
            if count<cfg['fixed_min_samples']:
                xy[:,i]=np.nan;status[:,i]=0
                warnings.append(f'{part}: only {count} confident detections; need {cfg["fixed_min_samples"]}. Fixed position left missing.')
            else:
                estimator=np.median if cfg['fixed_estimator']=='median' else np.mean
                xy[:,i]=estimator(values[valid,i,:2],axis=0);status[:,i]=3
            continue
        missing=np.flatnonzero(~valid)
        for gap in np.split(missing,np.flatnonzero(np.diff(missing)>1)+1):
            if not len(gap):continue
            left=int(gap[0])-1;right=int(gap[-1])+1
            if left<0 or right>=len(times):continue
            if values[left,i,2]<cfg['anchor_confidence'] or values[right,i,2]<cfg['anchor_confidence']:continue
            span=times[right]-times[left]
            if span>cfg['max_gap_seconds']+1e-12:continue
            fraction=(times[gap]-times[left])/span
            xy[gap,i]=xy[left,i]+fraction[:,None]*(xy[right,i]-xy[left,i]);status[gap,i]=2
    return xy,status,warnings
