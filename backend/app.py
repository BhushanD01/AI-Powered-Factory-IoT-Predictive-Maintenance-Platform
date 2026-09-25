"""
Simple FastAPI backend for the factory predictive maintenance platform.

This file exposes three lightweight endpoints:
1. Health check
2. Machine analytics from an uploaded CSV file
3. Maintenance prediction from analytics output
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import boto3
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from src.maintenance.bedrock_analyzer import (
    FactoryMaintenanceAnalyzer,
    InvalidModelOutputError,
)
from src.maintenance.analytics import FactoryEngineeringAnalytics

# Load environment variables from .env file
load_dotenv()

logger = logging.getLogger(__name__)


app = FastAPI(
    title="Factory Maintenance API",
    version="1.0.0",
    description="API for factory machine engineering analytics and maintenance reporting",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root() -> dict[str, str]:
    """Simple root endpoint so the frontend can confirm the backend is live."""
    return {"status": "ok", "service": "factory-maintenance-api", "message": "Backend is running"}


@app.get("/testing/health")
def health_check() -> dict[str, str]:
    """Simple health check endpoint for smoke testing."""
    return {"status": "ok", "service": "factory-maintenance-api"}


@app.post("/factory/analytics")
async def factory_analytics(
    csv_file: UploadFile = File(...),
    machine_id: str | None = Query(
        default=None,
        description="Specific machine to analyze. Defaults to the first machine in the file.",
    ),
    peer_scope: str = Query(
        default="machine_type",
        description="Peer comparison group: 'machine_type' or 'fleet'.",
    ),
) -> dict[str, Any]:
    """
    Generate engineering analytics from an uploaded factory sensor CSV file.

    Unlike the aircraft dataset, the factory dataset has one record per
    machine rather than a flight history, so analytics compares each machine
    against its peers (Table 3.1 of the manual) instead of its own past
    readings. Pass machine_id to pick a specific machine; otherwise the first
    machine found in the file is used.
    """
    if not csv_file.filename:
        raise HTTPException(status_code=400, detail="Please upload a CSV file.")

    if not csv_file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a valid CSV file (.csv).")

    if peer_scope not in ("machine_type", "fleet"):
        raise HTTPException(status_code=400, detail="peer_scope must be 'machine_type' or 'fleet'.")

    temp_path: str | None = None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as temp_file:
            content = await csv_file.read()
            temp_file.write(content)
            temp_path = temp_file.name

        analytics = FactoryEngineeringAnalytics(csv_path=temp_path)
        analytics.load_dataset()

        if machine_id is not None:
            target_machine_id = machine_id
        else:
            machine_list = analytics.list_machines()
            if not machine_list:
                raise ValueError("No machine data found in the uploaded file.")
            target_machine_id = machine_list[0]

        summary = analytics.generate_summary(
            machine_id=target_machine_id,
            peer_scope=peer_scope,
        )
        return {
            "message": "Analytics generated successfully",
            "machine_id": target_machine_id,
            "summary": summary.to_dict(),
        }

    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Analytics failed")
        raise HTTPException(status_code=500, detail=f"Analytics failed: {exc}") from exc
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


@app.post("/factory/maintenance-prediction")
def maintenance_prediction(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Generate a maintenance prediction report from the analytics output.

    The request body can be the full response from the analytics endpoint,
    and this function will extract the analytics payload automatically.
    """
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(status_code=400, detail="Please send the analytics output from the first API.")

    if isinstance(payload.get("summary"), dict):
        engineering_json = payload["summary"]
    elif isinstance(payload.get("engineering_json"), dict):
        engineering_json = payload["engineering_json"]
    else:
        engineering_json = payload

    if not isinstance(engineering_json, dict) or not engineering_json:
        raise HTTPException(status_code=400, detail="The analytics payload is empty or invalid.")

    base_dir = Path(__file__).resolve().parent
    manual_pdf_path = str(base_dir / "data" / "Factory_Simulator_2040_Maintenance_Manual.pdf")

    if not Path(manual_pdf_path).exists():
        raise HTTPException(
            status_code=404,
            detail=f"Maintenance manual not found at: {manual_pdf_path}.",
        )

    try:
        aws_region = os.getenv("AWS_REGION", "us-east-1")
        aws_access_key = os.getenv("AWS_ACCESS_KEY_ID")
        aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        bedrock_model_id = os.getenv("BEDROCK_MODEL_ID", "amazon.nova-pro-v1:0")

        boto3_kwargs = {
            "service_name": "bedrock-runtime",
            "region_name": aws_region,
        }

        # Only add explicit credentials if they are provided in .env
        # Otherwise, boto3 will fall back to default credential providers (e.g., ~/.aws/credentials)
        if aws_access_key and aws_access_key != "YOUR_ACCESS_KEY" and aws_secret_key and aws_secret_key != "YOUR_SECRET_KEY":
            boto3_kwargs["aws_access_key_id"] = aws_access_key
            boto3_kwargs["aws_secret_access_key"] = aws_secret_key

        bedrock_client = boto3.client(**boto3_kwargs)

        analyzer = FactoryMaintenanceAnalyzer(
            bedrock_client=bedrock_client,
            model_id=bedrock_model_id,
            manual_pdf_path=manual_pdf_path,
            temperature=0.2,
            max_tokens=4_000,
        )

        report = analyzer.analyze(engineering_json)
        return {"success": True, "report": report}

    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidModelOutputError as exc:
        # The model responded, but not with a usable report even after a retry.
        raise HTTPException(status_code=502, detail=f"Model did not return a valid report: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Maintenance prediction failed")
        raise HTTPException(
            status_code=500,
            detail=f"Maintenance prediction failed: {exc}",
        ) from exc


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)