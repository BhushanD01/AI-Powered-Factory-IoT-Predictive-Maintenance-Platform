"""
Step 1 analytics module for the Factory Predictive Maintenance Platform.

This module loads the factory sensor CSV, selects one machine record,
compares it against its peers (same machine type and the whole fleet), applies
the deterministic calculations defined in the SF Maintenance Manual, and
returns neutral statistical context as JSON-ready Python dictionaries.

This module never calls an LLM and never decides a maintenance status,
failure mode, or recommended action. Those decisions are left to the LLM,
which applies the manual (Tables 3.1, 3.2, 8.1, 8.2) to the facts prepared here.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Column configuration
# --------------------------------------------------------------------------- #

# Numeric fields compared against peers. Type-specific sensors (Laser_Intensity,
# Hydraulic_Pressure_bar, Coolant_Flow_L_min, Heat_Index) are blank for machine
# types that do not have the sensor; blank values are skipped automatically.
SENSOR_COLUMNS = [
    "Operational_Hours",
    "Temperature_C",
    "Vibration_mms",
    "Sound_dB",
    "Oil_Level_pct",
    "Coolant_Level_pct",
    "Power_Consumption_kW",
    "Last_Maintenance_Days_Ago",
    "Maintenance_History_Count",
    "Failure_History_Count",
    "Error_Codes_Last_30_Days",
    "Remaining_Useful_Life_days",
    "Laser_Intensity",
    "Hydraulic_Pressure_bar",
    "Coolant_Flow_L_min",
    "Heat_Index",
    "AI_Override_Events",  # only meaningful when AI_Supervision is True
]

# Column names may arrive in user-friendly variants; these are mapped to the
# canonical names used throughout this module.
COLUMN_ALIASES = {
    "machineid": "Machine_ID",
    "machine": "Machine_ID",
    "assetid": "Machine_ID",
    "machinetype": "Machine_Type",
    "assettype": "Machine_Type",
}

# Columns that are the "answer key" of the problem. They are hidden from the
# current_record by default (same idea as DECISION_COLUMNS in the aircraft
# module). Pass include_failure_flag=True to expose them.
LABEL_COLUMNS = {"Failure_Within_7_Days"}

# Manual, Chapter 8.1 / 8.2 - fitted on the 500,000-record fleet dataset.
RUL_INTERCEPT = 996.0
RUL_HOURS_COEF = -0.0099
RUL_TEMP_COEF = -0.50
RUL_VIB_COEF = -1.94
RISK_HORIZON_DAYS = 300.0
IMMINENT_FAILURE_RUL_DAYS = 6  # manual Rule E3: RUL of 6 or fewer days

# Manual, Table 3.3 - operational-hours life stages.
LIFE_STAGE_EDGES = [-1, 70_000, 80_000, 85_000, 90_000, 95_000, 100_000_000]
LIFE_STAGE_LABELS = [
    "0 - 70,000",
    "70,001 - 80,000",
    "80,001 - 85,000",
    "85,001 - 90,000",
    "90,001 - 95,000",
    "95,001 - 100,000+",
]

# Manual, Tables 3.1 and 3.2. Boundary values belong to the lower-severity band.
#   high_bad : (normal_max, warning_max)          -> above warning_max is CRITICAL
#   low_bad  : (normal_min, warning_min)          -> below warning_min is CRITICAL
#   two_sided: (normal_lo, normal_hi, warn_lo, warn_hi)
BAND_RULES: dict[str, tuple[str, tuple[float, ...]]] = {
    "Operational_Hours": ("high_bad", (70_000, 85_000)),
    "Temperature_C": ("high_bad", (75, 90)),
    "Vibration_mms": ("high_bad", (13, 18)),
    "Sound_dB": ("high_bad", (85, 95)),
    "Oil_Level_pct": ("low_bad", (50, 30)),
    "Coolant_Level_pct": ("low_bad", (40, 20)),
    "Power_Consumption_kW": ("high_bad", (230, 290)),
    "Last_Maintenance_Days_Ago": ("high_bad", (180, 300)),
    "Error_Codes_Last_30_Days": ("high_bad", (4, 6)),
    "Failure_History_Count": ("high_bad", (3, 5)),
    "AI_Override_Events": ("high_bad", (2, 4)),
    "Laser_Intensity": ("two_sided", (58, 92, 45, 105)),
    "Hydraulic_Pressure_bar": ("two_sided", (95, 145, 80, 160)),
    "Coolant_Flow_L_min": ("two_sided", (25, 58, 15, 70)),
    "Heat_Index": ("two_sided", (420, 580, 380, 620)),
}

# Remaining_Useful_Life_days is handled separately: NORMAL > 195, WARNING 45-195,
# CRITICAL < 45 (manual Table 3.1).

# Manual Rule E4 "primary" parameters vs. E1/E2 "secondary" parameters.
PRIMARY_PARAMETERS = {"Operational_Hours", "Temperature_C", "Vibration_mms"}
SECONDARY_PARAMETERS = {
    "Sound_dB",
    "Oil_Level_pct",
    "Coolant_Level_pct",
    "Power_Consumption_kW",
    "Last_Maintenance_Days_Ago",
    "Error_Codes_Last_30_Days",
    "Failure_History_Count",
    "AI_Override_Events",
    "Laser_Intensity",
    "Hydraulic_Pressure_bar",
    "Coolant_Flow_L_min",
    "Heat_Index",
}

# Manual, Table 4.2 - sensor plausibility limits: (column, predicate, description).
PLAUSIBILITY_RULES = [
    ("Vibration_mms", lambda v: v < 0, "below 0 (RMS vibration cannot be negative)"),
    ("Vibration_mms", lambda v: v > 25, "above 25 mm/s (verify sensor before escalating)"),
    ("Power_Consumption_kW", lambda v: v < 0, "below 0 (consumption cannot be negative)"),
    ("Temperature_C", lambda v: v < 0, "below 0 degC (outside physical operating range)"),
    ("Temperature_C", lambda v: v > 110, "above 110 degC (outside physical operating range)"),
    ("Sound_dB", lambda v: v > 105, "above 105 dB (verify sensor)"),
    ("Coolant_Flow_L_min", lambda v: v < 0, "below 0 (flow cannot be negative)"),
    ("Oil_Level_pct", lambda v: v == 0, "exactly 0.0 (empty reservoir or sensor dropout)"),
    ("Coolant_Level_pct", lambda v: v == 0, "exactly 0.0 (empty reservoir or sensor dropout)"),
]

# Manual, Table 2.1 - machine family and wear-critical components.
MACHINE_REGISTER: dict[str, tuple[str, str]] = {
    "CNC_Lathe": ("Machining & Fabrication", "Spindle bearings, chuck, ways, coolant pump"),
    "CNC_Mill": ("Machining & Fabrication", "Spindle, tool changer, ball screws, coolant pump"),
    "Grinder": ("Machining & Fabrication", "Spindle bearings, grinding wheel, dresser"),
    "Laser_Cutter": ("Machining & Fabrication", "Laser source, focusing optics, assist-gas nozzle"),
    "Press_Brake": ("Machining & Fabrication", "Ram guides, back-gauge drive, ram drive system"),
    "3D_Printer": ("Machining & Fabrication", "Print head/extruder, heated bed, motion axes"),
    "Hydraulic_Press": ("Forming", "Hydraulic pump, cylinders, seals, servo valves"),
    "Injection_Molder": ("Forming", "Hydraulic unit, screw and barrel, clamp unit"),
    "Boiler": ("Thermal", "Burner, tubes, feedwater pump, safety valve"),
    "Furnace": ("Thermal", "Heating elements or burners, refractory, temperature controls"),
    "Heat_Exchanger": ("Thermal", "Plates or tubes (fouling), gaskets, flow control"),
    "Dryer": ("Thermal", "Heating stage, blower, airflow dampers"),
    "Industrial_Chiller": ("Thermal", "Compressor, condenser, evaporator, circulation pump"),
    "Pump": ("Fluid & Process", "Impeller, mechanical seal, bearings"),
    "Compressor": ("Fluid & Process", "Compression stage, bearings, lubrication system, inlet filter"),
    "Mixer": ("Fluid & Process", "Agitator shaft, seals, gearbox"),
    "Valve_Controller": ("Fluid & Process", "Actuator, positioner, stem seals"),
    "AGV": ("Material Handling", "Drive wheels, traction motor and battery, navigation sensors"),
    "Forklift_Electric": ("Material Handling", "Mast and lift chain, traction motor, battery"),
    "Crane": ("Material Handling", "Hoist rope or chain, brakes, gearbox"),
    "Conveyor_Belt": ("Material Handling", "Belt, idlers, drive motor, gearbox"),
    "Shuttle_System": ("Material Handling", "Drive wheels and rails, motors, positioning encoders"),
    "Palletizer": ("Material Handling", "Gripper, lift axis, drive gearboxes"),
    "Robot_Arm": ("Robotics & Assembly", "Joint reducers, servo motors, cabling"),
    "Pick_and_Place": ("Robotics & Assembly", "Linear axes, vacuum gripper, feeders"),
    "Automated_Screwdriver": ("Robotics & Assembly", "Torque transducer, spindle, screw feeders"),
    "Labeler": ("Packaging", "Applicator, peel edge, drive rollers"),
    "Vacuum_Packer": ("Packaging", "Vacuum pump, seal bars, gaskets"),
    "Shrink_Wrapper": ("Packaging", "Heat tunnel, film drive, sealing wire"),
    "Carton_Former": ("Packaging", "Forming mandrel, glue system, drive cams"),
    "CMM": ("Inspection & Metrology", "Air bearings, probe, guideways"),
    "Vision_System": ("Inspection & Metrology", "Camera, lighting, optics"),
    "XRay_Inspector": ("Inspection & Metrology", "X-ray tube, detector, shielding interlocks"),
}


# --------------------------------------------------------------------------- #
# Result containers
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PeerAnalysis:
    """Neutral peer-comparison statistics for one numeric signal."""

    column: str
    latest_value: float
    peer_scope: str
    peer_group_size: int
    peer_average: float
    peer_median: float
    peer_std_dev: float
    change_from_average: float
    change_percent: float
    z_score: float
    percentile_in_peer_group: float
    percentile_in_fleet: float
    position_vs_peers: str


@dataclass(frozen=True)
class BandAssessment:
    """Manual band (Tables 3.1 / 3.2) for one parameter."""

    column: str
    value: float
    band: str  # NORMAL / WARNING / CRITICAL
    sensor_suspect: bool


@dataclass
class EngineeringSummary:
    """Final JSON-ready engineering summary."""

    machine_id: str
    machine_type: str
    machine_family: str | None
    wear_critical_components: str | None
    current_record: dict[str, Any]
    peer_scope: str
    peer_group_size: int
    peer_analysis: list[PeerAnalysis] = field(default_factory=list)
    derived_metrics: dict[str, Any] = field(default_factory=dict)
    life_stage_context: dict[str, Any] = field(default_factory=dict)
    band_assessment: list[BandAssessment] = field(default_factory=list)
    parameter_flags: dict[str, Any] = field(default_factory=dict)
    data_quality_flags: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "machine_id": self.machine_id,
            "machine_type": self.machine_type,
            "machine_family": self.machine_family,
            "wear_critical_components": self.wear_critical_components,
            "current_record": self.current_record,
            "peer_scope": self.peer_scope,
            "peer_group_size": self.peer_group_size,
            "peer_analysis": [item.__dict__ for item in self.peer_analysis],
            "derived_metrics": self.derived_metrics,
            "life_stage_context": self.life_stage_context,
            "band_assessment": [item.__dict__ for item in self.band_assessment],
            "parameter_flags": self.parameter_flags,
            "data_quality_flags": self.data_quality_flags,
        }


# --------------------------------------------------------------------------- #
# Analytics engine
# --------------------------------------------------------------------------- #

class FactoryEngineeringAnalytics:
    """Loads factory machine data and generates deterministic analytics."""

    def __init__(
        self,
        csv_path: str | Path,
        z_threshold: float = 1.0,
        include_failure_flag: bool = False,
        include_band_assessment: bool = True,
    ) -> None:
        """
        Args:
            csv_path: path to factory_sensor_simulator_2040.csv.
            z_threshold: |z-score| at or above which a value is labelled
                ABOVE_PEER_AVERAGE / BELOW_PEER_AVERAGE instead of TYPICAL.
            include_failure_flag: expose Failure_Within_7_Days in current_record.
                Default False, because it is the answer the LLM should work out.
            include_band_assessment: include the manual's Normal/Warning/Critical
                band for each parameter.
        """
        self.csv_path = Path(csv_path)
        self.z_threshold = z_threshold
        self.include_failure_flag = include_failure_flag
        self.include_band_assessment = include_band_assessment
        self.data: pd.DataFrame | None = None
        self._by_id: pd.DataFrame | None = None
        self._baseline_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
        self._life_stage_table: pd.DataFrame | None = None

    # ------------------------------------------------------------------ load
    def load_dataset(self) -> pd.DataFrame:
        """Load, clean, and validate the CSV dataset."""
        if not self.csv_path.exists():
            raise FileNotFoundError(f"Dataset not found: {self.csv_path}")

        logger.info("Loading factory sensor dataset: %s", self.csv_path)
        data = pd.read_csv(self.csv_path)
        data = self._clean_columns(data)
        self._validate_required_columns(data)

        data = data.dropna(subset=["Machine_ID", "Machine_Type"])
        data["Machine_ID"] = data["Machine_ID"].astype(str).str.strip()
        data["Machine_Type"] = data["Machine_Type"].astype(str).str.strip()

        if "AI_Supervision" in data.columns:
            data["AI_Supervision"] = self._to_bool(data["AI_Supervision"])
        if "Failure_Within_7_Days" in data.columns:
            data["Failure_Within_7_Days"] = self._to_bool(data["Failure_Within_7_Days"])

        for column in SENSOR_COLUMNS:
            if column in data.columns:
                data[column] = pd.to_numeric(data[column], errors="coerce")

        duplicates = int(data["Machine_ID"].duplicated().sum())
        if duplicates:
            logger.warning("%d duplicate Machine_ID rows found; the last is used.", duplicates)

        self.data = data.reset_index(drop=True)
        self._by_id = self.data.drop_duplicates("Machine_ID", keep="last").set_index(
            "Machine_ID", drop=False
        )
        self._baseline_cache.clear()
        self._life_stage_table = None
        return self.data

    # --------------------------------------------------------------- lookups
    def list_machines(self, machine_type: str | None = None) -> list[str]:
        """Return machine IDs, optionally limited to one machine type."""
        data = self._require_data()
        if machine_type is not None:
            data = data[data["Machine_Type"] == machine_type]
        return sorted(data["Machine_ID"].unique().tolist())

    def list_machine_types(self) -> list[str]:
        """Return all machine types in the loaded dataset."""
        return sorted(self._require_data()["Machine_Type"].unique().tolist())

    def sample_machine_ids(
        self, n: int = 5, machine_type: str | None = None, seed: int = 42
    ) -> list[str]:
        """Return a reproducible random sample of machine IDs (handy for testing)."""
        data = self._require_data()
        if machine_type is not None:
            data = data[data["Machine_Type"] == machine_type]
        return data["Machine_ID"].sample(n=min(n, len(data)), random_state=seed).tolist()

    def get_machine_record(self, machine_id: str) -> pd.Series:
        """Return the single record for one machine."""
        self._require_data()
        assert self._by_id is not None
        key = str(machine_id).strip()
        if key not in self._by_id.index:
            raise ValueError(f"No record found for machine: {machine_id}")
        return self._by_id.loc[key]

    def get_peer_group(self, machine_id: str, scope: str = "machine_type") -> pd.DataFrame:
        """Return peer records: same machine type ('machine_type') or all ('fleet')."""
        data = self._require_data()
        record = self.get_machine_record(machine_id)
        if scope == "fleet":
            return data
        if scope == "machine_type":
            return data[data["Machine_Type"] == record["Machine_Type"]]
        raise ValueError("scope must be 'machine_type' or 'fleet'")

    # --------------------------------------------------------------- summary
    def generate_summary(
        self,
        machine_id: str,
        peer_scope: str = "machine_type",
    ) -> EngineeringSummary:
        """
        Generate deterministic engineering analytics for one machine.

        This method does not call an LLM. It prepares trusted structured facts
        that can later be passed to Bedrock as engineering context.
        """
        record = self.get_machine_record(machine_id)
        machine_type = str(record["Machine_Type"])
        family, components = MACHINE_REGISTER.get(machine_type, (None, None))
        peers = self.get_peer_group(machine_id, scope=peer_scope)

        summary = EngineeringSummary(
            machine_id=str(record["Machine_ID"]),
            machine_type=machine_type,
            machine_family=family,
            wear_critical_components=components,
            current_record=self._series_to_json_ready_dict(record),
            peer_scope=peer_scope,
            peer_group_size=len(peers),
            peer_analysis=self._calculate_peer_analysis(record, peer_scope),
            derived_metrics=self._calculate_derived_metrics(record),
            life_stage_context=self._calculate_life_stage_context(record),
            data_quality_flags=self._check_data_quality(record),
        )

        if self.include_band_assessment:
            summary.band_assessment = self._assess_bands(record)
            summary.parameter_flags = self._summarize_parameter_flags(
                summary.band_assessment
            )
        return summary

    # ---------------------------------------------------------- peer analysis
    def _calculate_peer_analysis(
        self, record: pd.Series, peer_scope: str
    ) -> list[PeerAnalysis]:
        """Compare this machine's values against its peer group and the fleet."""
        analysis: list[PeerAnalysis] = []
        machine_type = str(record["Machine_Type"])
        scope_key = machine_type if peer_scope == "machine_type" else "__fleet__"

        for column in SENSOR_COLUMNS:
            if column not in record.index or pd.isna(record[column]):
                continue  # sensor not fitted to this machine type
            if column == "AI_Override_Events" and not bool(record.get("AI_Supervision", False)):
                continue  # overrides only exist for AI-supervised machines

            peer = self._baseline(scope_key, column)
            fleet = self._baseline("__fleet__", column)
            if peer is None or fleet is None:
                continue

            value = float(record[column])
            average = peer["mean"]
            std_dev = peer["std"]
            change = value - average
            change_percent = 0.0 if average == 0 else (change / abs(average)) * 100
            z_score = 0.0 if std_dev == 0 else change / std_dev

            analysis.append(
                PeerAnalysis(
                    column=column,
                    latest_value=round(value, 3),
                    peer_scope=peer_scope,
                    peer_group_size=peer["n"],
                    peer_average=round(average, 3),
                    peer_median=round(peer["median"], 3),
                    peer_std_dev=round(std_dev, 3),
                    change_from_average=round(change, 3),
                    change_percent=round(change_percent, 3),
                    z_score=round(z_score, 3),
                    percentile_in_peer_group=round(self._percentile(peer["sorted"], value), 1),
                    percentile_in_fleet=round(self._percentile(fleet["sorted"], value), 1),
                    position_vs_peers=self._position_label(z_score),
                )
            )
        return analysis

    def _baseline(self, scope_key: str, column: str) -> dict[str, Any] | None:
        """Cached statistics for one column within one peer group."""
        cache_key = (scope_key, column)
        if cache_key in self._baseline_cache:
            return self._baseline_cache[cache_key]

        data = self._require_data()
        if column not in data.columns:
            self._baseline_cache[cache_key] = None
            return None

        group = data if scope_key == "__fleet__" else data[data["Machine_Type"] == scope_key]
        if column == "AI_Override_Events" and "AI_Supervision" in group.columns:
            group = group[group["AI_Supervision"]]

        values = group[column].dropna().to_numpy(dtype=float)
        if values.size == 0:
            self._baseline_cache[cache_key] = None
            return None

        stats = {
            "n": int(values.size),
            "mean": float(values.mean()),
            "median": float(np.median(values)),
            "std": float(values.std(ddof=0)),
            "sorted": np.sort(values),
        }
        self._baseline_cache[cache_key] = stats
        return stats

    @staticmethod
    def _percentile(sorted_values: np.ndarray, value: float) -> float:
        """Percentage of the group with a value less than or equal to this one."""
        return float(np.searchsorted(sorted_values, value, side="right") / len(sorted_values) * 100)

    # ------------------------------------------------------- derived metrics
    def _calculate_derived_metrics(self, record: pd.Series) -> dict[str, Any]:
        """Apply the manual's Chapter 8 formulas (deterministic arithmetic only)."""
        needed = ["Operational_Hours", "Temperature_C", "Vibration_mms"]
        if any(c not in record.index or pd.isna(record[c]) for c in needed):
            return {"note": "RUL_est and Risk Score need hours, temperature and vibration."}

        hours = float(record["Operational_Hours"])
        temp = float(record["Temperature_C"])
        vib = float(record["Vibration_mms"])

        rul_est_raw = (
            RUL_INTERCEPT
            + RUL_HOURS_COEF * hours
            + RUL_TEMP_COEF * temp
            + RUL_VIB_COEF * vib
        )
        rul_est = max(rul_est_raw, 0.0)
        risk_score = 100.0 * min(1.0, max(0.0, 1.0 - rul_est_raw / RISK_HORIZON_DAYS))

        metrics: dict[str, Any] = {
            "rul_est_days": round(rul_est, 1),
            "risk_score_pct": round(risk_score, 1),
            "rul_model": "RUL_est = 996 - 0.0099*Hours - 0.50*Temp - 1.94*Vib",
            "risk_model": "Risk = 100 * clip(1 - RUL_est/300, 0, 1)",
        }

        recorded = record.get("Remaining_Useful_Life_days")
        if recorded is not None and not pd.isna(recorded):
            recorded = float(recorded)
            difference = recorded - rul_est
            metrics["recorded_rul_days"] = round(recorded, 1)
            metrics["recorded_minus_estimated_rul_days"] = round(difference, 1)
            # Manual Rule E6: differ by more than 100 days -> engineering review.
            metrics["rul_disagreement_exceeds_100_days"] = bool(abs(difference) > 100)
            # Manual Rule E3: RUL of 6 or fewer days means failure is imminent.
            metrics["rul_indicates_imminent_failure"] = bool(recorded <= IMMINENT_FAILURE_RUL_DAYS)

        return metrics

    # ------------------------------------------------------ life-stage context
    def _life_stage_stats(self) -> pd.DataFrame:
        """Fleet-wide observed failure rate per operational-hours band (Table 3.3)."""
        if self._life_stage_table is not None:
            return self._life_stage_table

        data = self._require_data()
        bands = pd.cut(data["Operational_Hours"], LIFE_STAGE_EDGES, labels=LIFE_STAGE_LABELS)
        grouped = data.groupby(bands, observed=True)
        table = pd.DataFrame({"records": grouped.size()})
        if "Failure_Within_7_Days" in data.columns:
            table["observed_failure_rate"] = grouped["Failure_Within_7_Days"].mean()
        if "Remaining_Useful_Life_days" in data.columns:
            table["median_recorded_rul_days"] = grouped["Remaining_Useful_Life_days"].median()
        self._life_stage_table = table
        return table

    def _calculate_life_stage_context(self, record: pd.Series) -> dict[str, Any]:
        """Where the machine sits in the fleet's wear-out curve (historical base rates)."""
        if "Operational_Hours" not in record.index or pd.isna(record["Operational_Hours"]):
            return {}

        hours = float(record["Operational_Hours"])
        label = pd.cut([hours], LIFE_STAGE_EDGES, labels=LIFE_STAGE_LABELS)[0]
        row = self._life_stage_stats().loc[label]
        fleet = self._baseline("__fleet__", "Operational_Hours")

        context: dict[str, Any] = {
            "operational_hours_band": str(label),
            "fleet_records_in_band": int(row["records"]),
            "hours_percentile_in_fleet": round(self._percentile(fleet["sorted"], hours), 1)
            if fleet
            else None,
        }
        if "observed_failure_rate" in row:
            context["fleet_observed_7_day_failure_rate_in_band_pct"] = round(
                float(row["observed_failure_rate"]) * 100, 2
            )
        if "median_recorded_rul_days" in row:
            context["fleet_median_recorded_rul_days_in_band"] = round(
                float(row["median_recorded_rul_days"]), 1
            )
        return context

    # -------------------------------------------------------------- bands
    def _assess_bands(self, record: pd.Series) -> list[BandAssessment]:
        """Classify each reported parameter per the manual's Tables 3.1 and 3.2."""
        suspect_columns = {flag["column"] for flag in self._check_data_quality(record)}
        results: list[BandAssessment] = []

        for column, (kind, limits) in BAND_RULES.items():
            if column not in record.index or pd.isna(record[column]):
                continue
            if column == "AI_Override_Events" and not bool(record.get("AI_Supervision", False)):
                continue
            value = float(record[column])
            results.append(
                BandAssessment(
                    column=column,
                    value=round(value, 3),
                    band=self._classify(value, kind, limits),
                    sensor_suspect=column in suspect_columns,
                )
            )

        recorded_rul = record.get("Remaining_Useful_Life_days")
        if recorded_rul is not None and not pd.isna(recorded_rul):
            rul = float(recorded_rul)
            band = "NORMAL" if rul > 195 else "WARNING" if rul >= 45 else "CRITICAL"
            results.append(BandAssessment("Remaining_Useful_Life_days", round(rul, 1), band, False))
        return results

    @staticmethod
    def _classify(value: float, kind: str, limits: tuple[float, ...]) -> str:
        if kind == "high_bad":
            normal_max, warning_max = limits
            return "NORMAL" if value <= normal_max else "WARNING" if value <= warning_max else "CRITICAL"
        if kind == "low_bad":
            normal_min, warning_min = limits
            return "NORMAL" if value >= normal_min else "WARNING" if value >= warning_min else "CRITICAL"
        normal_lo, normal_hi, warn_lo, warn_hi = limits  # two_sided
        if normal_lo <= value <= normal_hi:
            return "NORMAL"
        if warn_lo <= value <= warn_hi:
            return "WARNING"
        return "CRITICAL"

    @staticmethod
    def _summarize_parameter_flags(bands: list[BandAssessment]) -> dict[str, Any]:
        """List parameters by severity so the LLM can apply manual Rules E1, E2 and E4."""
        critical_primary = [b.column for b in bands if b.band == "CRITICAL" and b.column in PRIMARY_PARAMETERS]
        critical_secondary = [b.column for b in bands if b.band == "CRITICAL" and b.column in SECONDARY_PARAMETERS]
        warning = [b.column for b in bands if b.band == "WARNING"]
        return {
            "critical_primary_parameters": critical_primary,
            "critical_secondary_parameters": critical_secondary,
            "critical_secondary_count": len(critical_secondary),
            "warning_parameters": warning,
        }

    # ------------------------------------------------------- data quality
    @staticmethod
    def _check_data_quality(record: pd.Series) -> list[dict[str, Any]]:
        """Flag readings outside the manual's Table 4.2 plausibility limits."""
        flags: list[dict[str, Any]] = []
        for column, predicate, description in PLAUSIBILITY_RULES:
            if column not in record.index or pd.isna(record[column]):
                continue
            value = float(record[column])
            if predicate(value):
                flags.append({"column": column, "value": round(value, 3), "issue": description})
        return flags

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _clean_columns(data: pd.DataFrame) -> pd.DataFrame:
        """Normalize source headers into stable Python-friendly names."""
        data = data.copy()
        cleaned_columns = []

        for column in data.columns:
            column_name = str(column).strip()
            column_without_units = re.sub(r"\s*\([^)]*\)", "", column_name).strip()
            normalized_key = re.sub(r"[^a-z0-9]+", "", column_without_units.lower())

            if normalized_key in COLUMN_ALIASES:
                cleaned_columns.append(COLUMN_ALIASES[normalized_key])
            else:
                cleaned_columns.append(
                    re.sub(r"[^A-Za-z0-9]+", "_", column_without_units).strip("_")
                )

        data.columns = cleaned_columns
        return data

    @staticmethod
    def _validate_required_columns(data: pd.DataFrame) -> None:
        """Validate the minimum columns needed for machine analysis."""
        required_columns = {"Machine_ID", "Machine_Type", "Operational_Hours"}
        missing_columns = required_columns.difference(data.columns)
        if missing_columns:
            available_columns = ", ".join(data.columns)
            raise ValueError(
                f"Missing required columns: {sorted(missing_columns)}. "
                f"Available columns after cleanup: [{available_columns}]"
            )

    @staticmethod
    def _to_bool(series: pd.Series) -> pd.Series:
        """Convert True/False, 'True'/'False', or 1/0 values into booleans."""
        if series.dtype == bool:
            return series
        mapped = series.astype(str).str.strip().str.lower().map(
            {"true": True, "false": False, "1": True, "0": False, "1.0": True, "0.0": False}
        )
        return mapped.fillna(False).astype(bool)

    def _require_data(self) -> pd.DataFrame:
        if self.data is None:
            return self.load_dataset()
        return self.data

    def _position_label(self, z_score: float) -> str:
        """Convert a z-score into a simple peer-position label."""
        if z_score >= self.z_threshold:
            return "ABOVE_PEER_AVERAGE"
        if z_score <= -self.z_threshold:
            return "BELOW_PEER_AVERAGE"
        return "TYPICAL"

    @staticmethod
    def _to_native(value: Any) -> Any:
        """Convert numpy/pandas scalars into JSON-safe Python values."""
        if isinstance(value, np.generic):
            value = value.item()
        if isinstance(value, float):
            return None if np.isnan(value) else round(value, 3)
        return value

    def _series_to_json_ready_dict(self, row: pd.Series) -> dict[str, Any]:
        """Convert pandas values into JSON-safe Python values."""
        hidden = set() if self.include_failure_flag else LABEL_COLUMNS
        clean: dict[str, Any] = {}
        for key, value in row.to_dict().items():
            if key in hidden:
                continue
            if pd.isna(value):
                clean[key] = None  # type-specific sensor not fitted to this machine type
            elif hasattr(value, "isoformat"):
                clean[key] = value.isoformat()
            else:
                clean[key] = self._to_native(value)
        return clean


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    dataset_path = r"./backend/data/factory_sensor_data.csv"
    machine_id = sys.argv[1] if len(sys.argv) > 1 else "MC_000002"

    analytics = FactoryEngineeringAnalytics(dataset_path)
    analytics.load_dataset()

    summary = analytics.generate_summary(machine_id)
    print(json.dumps(summary.to_dict(), indent=2))