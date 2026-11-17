#!/bin/bash
cd /workspace/backend
export DATABASE_URL='postgresql+psycopg2://app@/query_review?host=/tmp&port=55440'
export REDIS_URL='redis://127.0.0.1:56379/0'
export JWT_SECRET='test-secret'
export ARTIFACT_DIR=/workspace/test-artifacts/gb-4101/artifacts
exec python3 -m uvicorn src.main:app --host 127.0.0.1 --port 18000
