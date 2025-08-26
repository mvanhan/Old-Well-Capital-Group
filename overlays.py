from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
import yaml

@dataclass
class OverlayConfig:
    events_file: Optional[str]
    block_during_events: bool
    pre_event_minutes: int
    post_event_minutes: int

def load_events(path: Optional[str]) -> List[dict]:
    if not path:
        return []
    try:
        with open(path, "r") as f:
            return yaml.safe_load(f) or []
    except Exception:
        return []

def in_event_window(now_utc: datetime, ev: dict, pre_min: int, post_min: int) -> bool:
    try:
        start = datetime.fromisoformat(ev["start"].replace("Z","+00:00"))
        end   = datetime.fromisoformat(ev["end"].replace("Z","+00:00"))
    except Exception:
        return False
    start -= timedelta(minutes=pre_min)
    end   += timedelta(minutes=post_min)
    return start <= now_utc <= end

def apply_event_overlay(
    scores: Dict[str, float],
    now_utc: datetime,
    cfg: OverlayConfig,
) -> Dict[str, float]:
    evs = load_events(cfg.events_file)
    if not evs:
        return scores
    out = dict(scores)
    for ev in evs:
        if not in_event_window(now_utc, ev, cfg.pre_event_minutes, cfg.post_event_minutes):
            continue
        symbols: List[str] = ev.get("symbols") or []
        if not symbols:
            if cfg.block_during_events:
                for k in out.keys():
                    out[k] = 0.0
            continue
        for s in symbols:
            if s in out and cfg.block_during_events:
                out[s] = 0.0
    return out

def apply_funding_bias(
    scores: Dict[str, float],
    funding_z: Dict[str, float],
    fade_threshold_z: float,
    carry_threshold_z: float,
) -> Dict[str, float]:
    """
    Positive funding_z => longs crowded -> damp score.
    Negative funding_z => shorts crowded -> boost score (spot-friendly long carry).
    """
    out = dict(scores)
    for sym, sc in scores.items():
        fz = funding_z.get(sym, 0.0)
        if fz >= fade_threshold_z:
            out[sym] = sc * 0.5
        elif fz <= carry_threshold_z:
            out[sym] = sc * 1.2
    return out
