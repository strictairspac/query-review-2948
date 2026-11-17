#!/bin/bash
cd /workspace/backend
export DATABASE_URL='postgresql+psycopg2://app@/query_review_final?host=/tmp&port=55440'
export REDIS_URL='redis://127.0.0.1:56380/0'
export JWT_SECRET='test-secret'
export ARTIFACT_DIR=/workspace/test-artifacts/gb-4101/final-artifacts
exec python3 -m src.worker
