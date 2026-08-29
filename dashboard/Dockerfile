# Dashboard image.
#
# **The build context is the REPO ROOT, not `dashboard/`** (FC-060 Layer 3).
# Every path below is therefore repo-relative, and `cloudbuild.yaml`'s
# `build-dashboard-image` step passes `-f dashboard/Dockerfile .` with no `dir:`.
# The reason is the two modules copied in the production stage: the sweep API has
# to refuse exactly what the sweep Job refuses, with the same reason strings, and
# the only way to guarantee that is to ship the same source file rather than a
# second copy of the rules. See D7, and the contract test
# `test_dashboard_image_ships_the_pure_scenario_modules`.

# Build stage for frontend
FROM node:20-alpine AS frontend-build

WORKDIR /app/frontend

# Copy frontend package files
COPY dashboard/frontend/package*.json ./
RUN npm ci

# Copy frontend source and build
COPY dashboard/frontend/ ./
RUN npm run build

# Production stage
FROM python:3.11-slim

WORKDIR /app

# Install Python dependencies
COPY dashboard/backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source
COPY dashboard/backend/ ./

# The two STDLIB-ONLY modules the sweep API shares with the backtest engine
# (FC-060 Layer 3). They are copied FLAT, as top-level modules, because their
# package's `__init__.py` imports the runner and would drag the whole engine —
# pandas, the Alpaca SDK, the simulator — into an image that has none of it.
# `services/sweeps.py` imports them under their package path first and falls back
# to these names; a test pins the fallback names against these destinations.
#
# Anything added here must stay dependency-free. A future edit to
# `overrides.py` that reaches for `Config`, PyYAML or structlog at RUNTIME breaks
# this image and nothing else — which is why both files say so at the top.
COPY src/backtesting/scenarios/overrides.py ./scenario_overrides.py
COPY src/backtesting/scenarios/identity.py ./scenario_identity.py

# Copy built frontend to static directory
COPY --from=frontend-build /app/frontend/dist ./static

# Set environment variables
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

# Run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
