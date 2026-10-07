# AI-Powered-Factory-IoT-Predictive-Maintenance-Platform

A production-oriented predictive maintenance platform that turns factory machine telemetry into peer-benchmarked engineering analytics, risk assessment, and AI-generated maintenance decisions. The platform combines a React frontend, FastAPI backend, deterministic engineering analytics, Amazon Bedrock Nova Pro, Docker, Kubernetes, Amazon ECR, Amazon EKS, and GitHub Actions CI/CD. <br>

## Overview
Industrial maintenance teams need more than raw sensor readings. They need to know:
- Is a machine operating outside expected conditions?
- How does it compare with similar machines?
- How much useful life is estimated to remain?
- What maintenance status should it receive?
- What failure modes are consistent with the observed condition?
- What should the maintenance team inspect or do next?

## Application Screenshots

### Factory Analytics Dashboard
![Factory Analytics Dashboard](backend/images/Screenshot%20%2877%29.png)
### Machine Health Analysis
![Machine Health Analysis](backend/images/Screenshot%20%2878%29.png)
### AI Maintenance Recommendation
![AI Maintenance Recommendation](backend/images/Screenshot%20%2879%29.png)

Note: The live cloud environment is currently offline to avoid ongoing AWS infrastructure costs. A recorded demo is provided below showing the deployed
application workflow, including CSV ingestion, engineering analytics, and AI-powered maintenance recommendations.

🎥 **[Watch the Demo](backend/images/demo.mp4)** --- Uploaded at 480p and 2× speed due to GitHub upload constraints.


## Repository Structure
```text
│
├── .github/
│   └── workflows/
│       └── ci_cd.yaml
├── backend/
│   ├── app.py
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── requirements.txt
│   ├── uv.lock
│   ├── data/
│   │   ├── factory_sensor_data.csv
│   │   └── Factory_Maintenance_Manual.pdf
│   └── src/
│       └── maintenance/
│           ├── analytics.py
│           └── bedrock_analyzer.py
├── frontend/
│   ├── Dockerfile
│   ├── package.json
│   ├── package-lock.json
│   ├── vite.config.js
│   ├── index.html
│   ├── .env.example
│   └── src/
│       ├── App.jsx
│       ├── main.jsx
│       ├── styles.css
│       ├── api/
│       │   └── client.js
│       └── components/
│           └── PanelCard.jsx
│
└── kubernetes_deployment/
    ├── backend_deployment.yaml
    └── frontend_deployment.yaml
```


## Configuration

### Backend
AWS configuration is read from environment variables:
```text
AWS_REGION
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
```

BEDROCK_MODEL_ID defaults to:
```text
amazon.nova-pro-v1:0
```
### Frontend
Create:
```text
frontend/.env
```
with:
```text
VITE_API_BASE_URL=http://localhost:8000
```
For a deployed environment, set this to the deployed backend service URL.

## Local Setup & Configuration

### Backend
```bash
cd backend

python -m venv .venv
```
Activate the environment and install dependencies:
```bash
pip install -r requirements.txt
```
Start FastAPI:
```bash
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```
The API will be available at:
```text
http://localhost:8000
```
Swagger UI/ Interactive documentation:
```text
http://localhost:8000/docs
```
### Frontend
```bash
cd frontend
npm install
npm run dev
```
Configure:
```text
VITE_API_BASE_URL=http://localhost:8000
```
The Vite development server runs on port 3000.

### Docker
### Backend
```bash
docker build -t factory-maintenance-backend ./backend
docker run -p 8000:8000 \
  -e AWS_REGION=<region> \
  -e AWS_ACCESS_KEY_ID=<key> \
  -e AWS_SECRET_ACCESS_KEY=<secret> \
  factory-maintenance-backend
```
### Frontend
```text
docker build -t factory-maintenance-frontend ./frontend
docker run -p 3000:3000 \
  factory-maintenance-frontend
```