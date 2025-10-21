Local compose setup for qa-packet-validator

This compose file spins up a local stack to test the full flow (web -> enqueue -> worker -> S3/localstack).

Prerequisites
- Docker and docker-compose installed locally

Files created
- `docker-compose.yml` - compose stack with redis, localstack, web, and worker
- `.env.example` - sample environment variables to copy to `.env`

Quick start
1. Copy `.env.example` to `.env` and edit if necessary:

   cp .env.example .env

2. Build and start the stack:

   docker-compose up --build

3. Open the web UI at http://localhost:3000 and upload a sample PDF.

4. Watch the logs for the worker picking up jobs and producing `exports/` output or S3 objects in localstack.

Notes
- localstack exposes S3-compatible API on port 4566. The docker-compose file pre-sets `AWS_*` env vars for localstack usage.
- Exports are still written to the local `app/exports/` directory by default. When S3 is configured, files will be uploaded to S3 (localstack) and presigned URLs returned.
- If you make changes to the Python code, the container mounts the workspace so code changes are picked up by restarting the web/worker containers.
