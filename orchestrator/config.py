"""
Run configuration schema. Every AOI/region-specific value used anywhere in
the pipeline must live in this schema and be read from a run's
config_used.yaml at run time -- nothing region-specific should ever be a
hardcoded constant in a stage module. This is what makes the pipeline work
for any AOI rather than one specific place.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator


class AOI(BaseModel):
    bbox: list[float] = Field(..., min_length=4, max_length=4)  # [lon_min, lat_min, lon_max, lat_max]

    @field_validator("bbox")
    @classmethod
    def _valid_bbox(cls, v):
        lon_min, lat_min, lon_max, lat_max = v
        if not (lon_min < lon_max and lat_min < lat_max):
            raise ValueError("bbox must be [lon_min, lat_min, lon_max, lat_max] with min < max")
        if not (-180 <= lon_min and lon_max <= 180):
            raise ValueError("longitude out of range")
        if not (-90 <= lat_min and lat_max <= 90):
            raise ValueError("latitude out of range")
        return v


class DateRange(BaseModel):
    start: str  # YYYY-MM-DD
    end: str


class Sentinel1(BaseModel):
    relative_orbit: Optional[int] = None   # filled in from the "search tracks" step, not assumed
    flight_direction: Optional[str] = None  # "ASCENDING" or "DESCENDING" -- chosen per-AOI, not fixed
    polarization: str = "VV"


class DEMConfig(BaseModel):
    source: str = "glo_30"
    # ISCE2's topo step needs the DEM to cover the full sensor swath
    # footprint of each retained burst, not just the output AOI -- this
    # requirement comes from the satellite geometry, not the AOI size, so
    # even a tiny AOI needs generous padding. 0.1 was proven too small: a
    # single-burst test AOI with padding_deg=0.1 never finished its first
    # burst in 20+ minutes, while topo's own log reported needing roughly
    # 0.7 deg lon / 0.3 deg lat beyond the bbox to fully converge.
    padding_deg: float = 1.0


class NetworkConfig(BaseModel):
    connections: int = 3
    azimuth_looks: int = 7
    range_looks: int = 19
    run16_parallelism: int = 1   # sequential by default -- safe regardless of AOI size
    num_proc: int = 1            # serial by default -- 2/2+ caused CPU oversubscription in live testing;
    num_proc4topo: int = 1       # override upward at Gate A for a specific run if the host machine can take it


class MintPyConfig(BaseModel):
    reference_lalo: Optional[list[float]] = None   # null until Gate B approval -- never auto-committed
    reference_search_radius_km: float = 5.0          # starting default; picker widens automatically if empty
    reliability_threshold: float = 0.5                # coherence threshold -- a config value, not hardcoded
    unwrap_error_method: str = "bridging+phase_closure"
    deramp: str = "linear"
    tropo_enabled: bool = False
    tropo_method: str = "pyaps"
    tropo_weather_model: str = "ERA5"
    topographic_residual: bool = True


class OutputConfig(BaseModel):
    base_dir: str = "~/insar-automation/runs"


class RunConfig(BaseModel):
    run_id: str
    aoi: AOI
    date_range: DateRange
    sentinel1: Sentinel1 = Sentinel1()
    dem: DEMConfig = DEMConfig()
    network: NetworkConfig = NetworkConfig()
    mintpy: MintPyConfig = MintPyConfig()
    output: OutputConfig = OutputConfig()

    @property
    def run_dir(self) -> Path:
        base = Path(os.path.expanduser(self.output.base_dir))
        return base / self.run_id

    @classmethod
    def from_yaml(cls, path: str) -> "RunConfig":
        with open(os.path.expanduser(path)) as f:
            data = yaml.safe_load(f)
        return cls(**data)

    def to_yaml(self, path: str) -> None:
        path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            yaml.safe_dump(self.model_dump(), f, sort_keys=False, default_flow_style=False)

    def freeze_into_run_dir(self) -> Path:
        """Write the exact config used for this run into runs/<id>/config_used.yaml,
        for reproducibility -- distinct from any editable template."""
        out = self.run_dir / "config_used.yaml"
        self.to_yaml(str(out))
        return out
