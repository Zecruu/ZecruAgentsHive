# Multi-stage build: the AgentsHive Python server + the companion webapp it serves
# at /app. Stage 1 builds apps/web (Node); stage 2 is the Python runtime, mirroring
# the previous Nixpacks setup EXACTLY (Python 3.11, `pip install .` via hatchling,
# `python run.py` with run.py's ROOT anchoring) so server behavior is unchanged.
# The ONLY net-new piece is copying the built webapp dist into the runtime image.

# ---- Stage 1: build the companion webapp (apps/web) ----
FROM node:20-slim AS webapp
WORKDIR /webapp
# Public build-time config, passed as --build-arg (set as Railway build vars; the
# anon key is publishable/browser-safe). VITE_AGENTSHIVE_URL='' → the bundle calls
# the API same-origin. Referenced as ARGs only — values are never hardcoded here.
ARG VITE_SUPABASE_URL
ARG VITE_SUPABASE_ANON_KEY
ARG VITE_AGENTSHIVE_URL=""
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web/ ./
RUN VITE_SUPABASE_URL="$VITE_SUPABASE_URL" \
    VITE_SUPABASE_ANON_KEY="$VITE_SUPABASE_ANON_KEY" \
    VITE_AGENTSHIVE_URL="$VITE_AGENTSHIVE_URL" \
    npm run build

# ---- Stage 2: Python runtime (mirrors the prior Nixpacks setup) ----
FROM python:3.11-slim AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1
# Install the server exactly like the old `pip install .` (hatchling builds the
# wheel from pyproject; psycopg[binary] + cryptography ship manylinux wheels, so
# no apt build deps are needed). Copy the build inputs first for layer caching.
COPY pyproject.toml ./
COPY src/ ./src/
COPY run.py ./
RUN pip install --no-cache-dir .
# Bring in the built webapp so the server serves it at /app. Pin the location
# explicitly (env override) so it never depends on __file__ resolution after the
# pip install — main.py reads AGENTSHIVE_WEBAPP_DIST first.
COPY --from=webapp /webapp/dist ./apps/web/dist
ENV AGENTSHIVE_WEBAPP_DIST=/app/apps/web/dist
# Same start command as the Nixpacks deploy. run.py anchors sys.path to ./src.
CMD ["python", "run.py"]
