"""
Amazon Bedrock maintenance analyzer for the Factory Predictive Maintenance Platform.

This module sends deterministic engineering analytics plus the full internal
SF maintenance manual PDF to Amazon Bedrock Nova Pro using the Converse API.
The model is asked to return a structured maintenance report as JSON only.

Pipeline position
-----------------
Step 1  factory_engineering_analytics.py  -> deterministic facts (JSON-ready dict)
Step 2  this module                       -> LLM applies the manual to those facts
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Protocol

from botocore.exceptions import BotoCoreError, ClientError


logger = logging.getLogger(__name__)


# Bedrock accepts PDF documents of up to 4.5 MB in the Converse API.
MAX_MANUAL_BYTES = 4_500_000

# Allowed values, kept in one place so the prompt and the validator cannot drift.
HEALTH_STATUSES = ("HEALTHY", "MONITOR", "MAINTENANCE_DUE", "CRITICAL")
RISK_LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL", "UNKNOWN")
DECISIONS = (
    "CONTINUE_OPERATION",
    "CONTINUE_WITH_MONITORING",
    "SCHEDULE_MAINTENANCE",
    "REMOVE_FROM_SERVICE",
)
PRIORITIES = ("P1", "P2", "P3", "P4")
REQUIRED_KEYS = (
    "machine_id",
    "health_status",
    "risk_level",
    "priority",
    "final_operating_decision",
    "status_derivation",
    "threshold_violations",
    "detected_failure_modes",
    "root_cause",
    "maintenance_actions",
    "inspection_checklist",
    "work_order",
    "confidence",
)
STATUS_RANK = {status: rank for rank, status in enumerate(HEALTH_STATUSES)}


# The output contract is a plain string (not an f-string), so JSON braces do not
# need to be doubled. It is inserted into the prompt in build_prompt().
OUTPUT_SCHEMA = """
{
  "machine_id": "string",
  "machine_type": "string",
  "machine_family": "string",
  "health_status": "HEALTHY | MONITOR | MAINTENANCE_DUE | CRITICAL",
  "risk_level": "LOW | MEDIUM | HIGH | CRITICAL | UNKNOWN",
  "risk_score_pct": "number copied from derived_metrics, or null",
  "estimated_rul_days": "number copied from derived_metrics, or null",
  "priority": "P1 | P2 | P3 | P4",
  "safe_to_continue_operation": true,
  "final_operating_decision": {
    "decision": "CONTINUE_OPERATION | CONTINUE_WITH_MONITORING | SCHEDULE_MAINTENANCE | REMOVE_FROM_SERVICE",
    "can_operate_now": true,
    "ui_statement": "string",
    "target_response_time": "string taken from the manual",
    "required_before_continued_operation": "string",
    "decision_rationale": "string"
  },
  "overall_summary": "string",
  "status_derivation": {
    "risk_matrix_status": "HEALTHY | MONITOR | MAINTENANCE_DUE | CRITICAL",
    "escalation_rules_applied": [
      {"rule": "E1 | E2 | E3 | E4 | E5 | E6", "reason": "string"}
    ],
    "final_status_reason": "string"
  },
  "threshold_violations": [
    {
      "parameter": "string",
      "observed_value": "number or string",
      "manual_threshold": "string",
      "severity": "LOW | MEDIUM | HIGH | CRITICAL | UNKNOWN",
      "manual_reference": "string",
      "explanation": "string"
    }
  ],
  "detected_failure_modes": [
    {
      "failure_mode": "string",
      "manual_reference": "string",
      "severity": "string",
      "evidence": ["string"]
    }
  ],
  "root_cause": {
    "most_likely_cause": "string",
    "supporting_evidence": ["string"],
    "manual_reference": "string"
  },
  "maintenance_actions": [
    {
      "priority": 1,
      "action": "string",
      "reason": "string",
      "manual_reference": "string"
    }
  ],
  "inspection_checklist": [
    {
      "step": 1,
      "inspection_item": "string",
      "acceptance_criteria": "string",
      "manual_reference": "string"
    }
  ],
  "data_quality_notes": ["string"],
  "work_order": {
    "title": "string",
    "machine_id": "string",
    "work_order_code": "INSP-A | INSP-B | INSP-C | INSP-D | WO-R | WO-X | WO-O | WO-L | WO-S",
    "priority": "P1 | P2 | P3 | P4",
    "tasks": ["string"],
    "required_parts_or_tools": ["string"],
    "target_completion": "string taken from the manual",
    "estimated_maintenance_category": "string"
  },
  "confidence": {
    "score": 0.0,
    "rationale": "string",
    "missing_information": ["string"]
  }
}
"""


class BedrockRuntimeClient(Protocol):
    """Minimal protocol for the boto3 Bedrock Runtime client."""

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        """Call the Bedrock Converse API."""


class InvalidModelOutputError(ValueError):
    """The model answered, but not with a usable report (bad JSON or schema)."""


class FactoryMaintenanceAnalyzer:
    """
    Generate AI maintenance reports using Amazon Bedrock and the SF manual PDF.
    """

    def __init__(
        self,
        bedrock_client: BedrockRuntimeClient,
        model_id: str,
        manual_pdf_path: str | Path,
        temperature: float = 0.2,
        max_tokens: int = 4_000,
        max_retries: int = 1,
    ) -> None:
        """
        Args:
            max_tokens: the report schema is long; too small a value truncates the
                JSON. Nova Pro allows roughly 5,000 output tokens.
            max_retries: extra attempts if the model returns invalid JSON/schema.
                Network and permission errors are never retried here.
        """
        self.bedrock_client = bedrock_client
        self.model_id = model_id
        self.manual_pdf_path = Path(manual_pdf_path)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_retries = max_retries

    def load_manual(self) -> bytes:
        """Load the complete factory maintenance manual PDF as bytes."""
        if not self.manual_pdf_path.exists():
            raise FileNotFoundError(
                f"Maintenance manual not found: {self.manual_pdf_path}"
            )

        if self.manual_pdf_path.suffix.lower() != ".pdf":
            raise ValueError(
                "Maintenance manual must be a PDF file. "
                f"Received: {self.manual_pdf_path}"
            )

        manual_bytes = self.manual_pdf_path.read_bytes()
        if len(manual_bytes) > MAX_MANUAL_BYTES:
            raise ValueError(
                f"Manual is {len(manual_bytes):,} bytes; Bedrock documents must be "
                f"under {MAX_MANUAL_BYTES:,} bytes."
            )

        logger.info("Loaded maintenance manual: %s", self.manual_pdf_path)
        return manual_bytes

    def build_prompt(self, engineering_analytics: dict[str, Any]) -> str:
        """Build the model prompt from engineering analytics JSON."""
        analytics_json = json.dumps(
            engineering_analytics,
            indent=2,
            ensure_ascii=False,
            default=str,
        )

        return f"""
You are a Senior Reliability and Maintenance Engineer for a smart factory.

Your task is to generate a professional machine maintenance engineering report
using two inputs:

1. Engineering Analytics JSON provided below.
2. The attached internal SF Smart Factory Equipment Maintenance &
   Condition Monitoring Manual PDF.

Important rules:
- Use only thresholds, formulas, rules, failure modes, procedures and maintenance
  actions found in the attached manual. Do not invent any of them. If something
  you need is not in the manual, say so explicitly in the JSON output.
- The analytics JSON contains pre-computed facts. Use derived_metrics.rul_est_days
  and derived_metrics.risk_score_pct exactly as given; do not recompute them.
- Check the band_assessment entries against the manual's Table 3.1 and Table 3.2.
  If you find a disagreement, follow the manual and mention it in data_quality_notes.
- Determine the maintenance status in this order:
  1. Map risk_score_pct to a status using the manual's Maintenance Decision Matrix
     (Table 8.1).
  2. Apply the Condition Escalation Rules (Table 8.2, Rules E1 to E6), using
     parameter_flags, derived_metrics and data_quality_flags.
  3. The final health_status is the highest status produced by any rule. List every
     rule you applied in status_derivation.escalation_rules_applied.
- Take the priority (P1 to P4), the target response time and the work order codes
  from the manual (Table 8.3, Chapter 9 and Section 10.3).
- The final_operating_decision must be consistent with the manual's status:
  HEALTHY -> CONTINUE_OPERATION, MONITOR -> CONTINUE_WITH_MONITORING,
  MAINTENANCE_DUE -> SCHEDULE_MAINTENANCE, CRITICAL -> REMOVE_FROM_SERVICE.
  For risk_level use HEALTHY -> LOW, MONITOR -> MEDIUM, MAINTENANCE_DUE -> HIGH,
  CRITICAL -> CRITICAL.
- Match the observed pattern to the failure modes in manual Chapter 5 and cite the
  section number (for example "Section 5.1.1"). Prefer the manual's detection logic.
- peer_analysis and life_stage_context are statistical context only. Use them to
  judge how unusual a reading is and to support the root cause. Never use them in
  place of the manual's thresholds.
- A null value for Laser_Intensity, Hydraulic_Pressure_bar, Coolant_Flow_L_min or
  Heat_Index means the sensor is not fitted to this machine type. It is not a fault.
- If data_quality_flags is not empty, follow the manual's sensor-fault guidance
  (Section 5.1.6, Table 4.2, Chapter 6.7): mention it, treat the status as
  provisional, and never downgrade the status because of a suspect reading.
- Prioritize maintenance actions when multiple actions apply.
- Return JSON only. Do not include Markdown, prose outside JSON, or code fences.

Required analysis:
- Health Status and Risk Level
- Status Derivation (risk matrix status, escalation rules)
- Threshold Violations
- Detected Failure Modes
- Root Cause
- Maintenance Recommendation and Final Operating Decision
- Inspection Checklist
- Data Quality Notes
- Generated Work Order
- Confidence

Engineering Analytics JSON:
{analytics_json}

Return exactly one JSON object with this schema:
{OUTPUT_SCHEMA}
""".strip()

    def analyze(self, engineering_analytics: dict[str, Any]) -> dict[str, Any]:
        """Generate a structured AI maintenance report from analytics and PDF."""
        prompt = self.build_prompt(engineering_analytics)
        manual_bytes = self.load_manual()

        conversation = [
            {
                "role": "user",
                "content": [
                    {"text": prompt},
                    {
                        "document": {
                            "format": "pdf",
                            "name": self._document_name(),
                            "source": {"bytes": manual_bytes},
                        }
                    },
                ],
            }
        ]

        last_error: InvalidModelOutputError | None = None
        for attempt in range(1, self.max_retries + 2):
            response = self._call_bedrock(conversation)
            try:
                response_text = self._extract_response_text(response)
                report = self._parse_json_response(response_text)
                self._validate_report(report)
            except InvalidModelOutputError as exc:
                last_error = exc
                logger.warning("Attempt %d produced an invalid report: %s", attempt, exc)
                continue

            report["validation_warnings"] = self._check_status_consistency(
                engineering_analytics, report
            )
            return report

        raise InvalidModelOutputError(
            f"Model did not return a valid report after {self.max_retries + 1} attempt(s)"
        ) from last_error

    def _call_bedrock(self, conversation: list[dict[str, Any]]) -> dict[str, Any]:
        """Invoke the Converse API and translate AWS errors into one clear error."""
        try:
            logger.info("Invoking Bedrock model: %s", self.model_id)
            return self.bedrock_client.converse(
                modelId=self.model_id,
                messages=conversation,
                inferenceConfig={
                    "maxTokens": self.max_tokens,
                    "temperature": self.temperature,
                },
            )
        except (ClientError, BotoCoreError) as exc:
            logger.exception("Bedrock invocation failed")
            raise RuntimeError(
                f"Unable to invoke Bedrock model '{self.model_id}'"
            ) from exc

    def _document_name(self) -> str:
        """Return a Bedrock-safe document name (letters, digits, spaces, - ( ) [ ])."""
        name = re.sub(r"[^A-Za-z0-9 \-\(\)\[\]]", " ", self.manual_pdf_path.stem)
        name = re.sub(r"\s+", " ", name).strip()
        return name or "Maintenance Manual"

    @staticmethod
    def _extract_response_text(response: dict[str, Any]) -> str:
        """Extract text from a Bedrock Converse response."""
        if response.get("stopReason") == "max_tokens":
            raise ValueError(
                "Model output was cut off (stopReason=max_tokens). "
                "Increase max_tokens; retrying with the same limit will not help."
            )

        try:
            content = response["output"]["message"]["content"]
        except KeyError as exc:
            raise ValueError("Bedrock response did not contain message content") from exc

        text_parts = [
            block["text"]
            for block in content
            if isinstance(block, dict) and "text" in block
        ]
        response_text = "\n".join(text_parts).strip()

        if not response_text:
            raise InvalidModelOutputError("Bedrock returned an empty response")

        return response_text

    @staticmethod
    def _parse_json_response(response_text: str) -> dict[str, Any]:
        """
        Parse the JSON-only model response.

        Models occasionally wrap JSON in ```json fences or add a sentence around it
        despite instructions, so fences are stripped and, as a last resort, the text
        between the first '{' and the last '}' is parsed.
        """
        text = response_text.strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                logger.error("Model returned non-JSON response: %s", response_text)
                raise InvalidModelOutputError("Bedrock response was not valid JSON")
            try:
                parsed = json.loads(text[start : end + 1])
            except json.JSONDecodeError as exc:
                logger.error("Model returned non-JSON response: %s", response_text)
                raise InvalidModelOutputError("Bedrock response was not valid JSON") from exc

        if not isinstance(parsed, dict):
            raise InvalidModelOutputError("Bedrock response JSON must be an object")

        return parsed

    @staticmethod
    def _validate_report(report: dict[str, Any]) -> None:
        """Check required keys and the allowed values of the key enum fields."""
        missing = [key for key in REQUIRED_KEYS if key not in report]
        if missing:
            raise InvalidModelOutputError(f"Report is missing required keys: {missing}")

        if report["health_status"] not in HEALTH_STATUSES:
            raise InvalidModelOutputError(
                f"Invalid health_status: {report['health_status']!r}"
            )
        if report["risk_level"] not in RISK_LEVELS:
            raise InvalidModelOutputError(f"Invalid risk_level: {report['risk_level']!r}")
        if report["priority"] not in PRIORITIES:
            raise InvalidModelOutputError(f"Invalid priority: {report['priority']!r}")

        decision = report["final_operating_decision"]
        if not isinstance(decision, dict) or decision.get("decision") not in DECISIONS:
            raise InvalidModelOutputError("Invalid final_operating_decision.decision")

    @staticmethod
    def _check_status_consistency(
        analytics: dict[str, Any], report: dict[str, Any]
    ) -> list[str]:
        """
        Safety net: compare the model's status with the minimum status implied by
        the manual's Table 8.1 and Rules E1 to E4. This NEVER changes the report;
        it only returns warnings for a human or a dashboard to see.
        """
        metrics = analytics.get("derived_metrics") or {}
        flags = analytics.get("parameter_flags") or {}
        minimum = 0
        reasons: list[str] = []

        risk = metrics.get("risk_score_pct")
        if isinstance(risk, (int, float)):
            risk_rank = 0 if risk <= 35 else 1 if risk <= 60 else 2 if risk <= 85 else 3
            if risk_rank > minimum:
                minimum = risk_rank
            if risk_rank:
                reasons.append(f"risk_score_pct {risk} maps to {HEALTH_STATUSES[risk_rank]}")

        if metrics.get("rul_indicates_imminent_failure"):
            minimum = max(minimum, 3)
            reasons.append("recorded RUL of 6 days or fewer (Rule E3)")
        if flags.get("critical_primary_parameters"):
            minimum = max(minimum, 2)
            reasons.append("a primary parameter is Critical (Rule E4)")
        secondary = int(flags.get("critical_secondary_count") or 0)
        if secondary >= 2:
            minimum = max(minimum, 2)
            reasons.append("two or more secondary parameters are Critical (Rule E2)")
        elif secondary == 1:
            minimum = max(minimum, 1)
            reasons.append("one secondary parameter is Critical (Rule E1)")

        warnings: list[str] = []
        status = report.get("health_status")
        if STATUS_RANK.get(status, 0) < minimum:
            warnings.append(
                f"health_status {status} is lower than the minimum "
                f"{HEALTH_STATUSES[minimum]} implied by: {'; '.join(reasons)}"
            )
        if status == "CRITICAL" and report["final_operating_decision"]["decision"] != "REMOVE_FROM_SERVICE":
            warnings.append("CRITICAL status should map to REMOVE_FROM_SERVICE")
        if status == "HEALTHY" and report["final_operating_decision"]["decision"] != "CONTINUE_OPERATION":
            warnings.append("HEALTHY status should map to CONTINUE_OPERATION")
        return warnings


if __name__ == "__main__":
    import boto3

    # Adjust this import if your analytics file lives in another package.
    from analytics import FactoryEngineeringAnalytics

    logging.basicConfig(level=logging.INFO)

    base_dir = Path(__file__).resolve().parents[3]
    manual_pdf_path = base_dir / "backend/data/Factory_Maintenance_Manual.pdf"
    dataset_path = base_dir / "backend/data/factory_sensor_data.csv"
    machine_id = sys.argv[1] if len(sys.argv) > 1 else "MC_000002"

    if not manual_pdf_path.exists():
        raise FileNotFoundError(f"Maintenance manual not found: {manual_pdf_path}")

    # Step 1: deterministic analytics for one machine.
    # (You can also paste a saved analytics JSON dict here instead.)
    analytics = FactoryEngineeringAnalytics(dataset_path)
    analytics.load_dataset()
    engineering_json = analytics.generate_summary(machine_id).to_dict()

    # Step 2: LLM applies the manual to those facts.
    bedrock_client = boto3.client("bedrock-runtime", region_name="us-east-1")
    analyzer = FactoryMaintenanceAnalyzer(
        bedrock_client=bedrock_client,
        model_id="amazon.nova-pro-v1:0",
        manual_pdf_path=manual_pdf_path,
        temperature=0.2,
        max_tokens=4000,
    )

    result = analyzer.analyze(engineering_json)
    print(json.dumps(result, indent=2))