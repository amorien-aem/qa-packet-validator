Render + Redis/RQ + S3 deployment notes

Goal
----
Deploy the QA Packet Validator so uploads are processed in background workers, progress is shared across web and worker instances, and output files are persisted (S3 recommended) to survive ephemeral containers.

Key pieces
----------
- Web service: Flask app (entry: `app/app.py`) served with Gunicorn or the Docker CMD.
- Background worker: RQ worker process that dequeues jobs from Redis and runs validation.
- Redis: central progress store and RQ broker.
- S3 (optional but recommended): persist outputs and provide presigned download links.
- Tesseract: system-level binary required for OCR fallback (install in Dockerfile or on host).

Dockerfile (important bits)
--------------------------
- Ensure system packages include `tesseract-ocr` and fonts. Example for Debian/Ubuntu:

```Dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y tesseract-ocr libtiff5 libjpeg62-turbo libopenjp2-7 poppler-utils && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=3000
CMD ["gunicorn", "app.app:app", "-b", "0.0.0.0:3000", "-w", "2"]
```

Render configuration
--------------------
- Create two services on Render (or one service + a worker):
  1. Web Service: build from Dockerfile above. Set environment variables:
     - `REDIS_URL` -> redis://... (point at your Redis instance)
     - `S3_BUCKET` (if using S3)
     - `S3_PREFIX` (optional)
  2. Worker (Background Worker): A separate service or background worker running an RQ worker. Command to run inside the same Docker image:
     - `rq worker -u $REDIS_URL default`

Important: Both the Web service and the Worker must use the exact same `REDIS_URL` so progress updates and job enqueuing are visible to both.

Redis
-----
- Use a managed Redis on Render or a hosted Redis provider. Ensure network connectivity from both Web and Worker.
- If you cannot use Redis, the app will fallback to an in-memory progress store, but that does not work across multiple containers and will cause the `done` flag to be missing between processes.

RQ worker
---------
- The app enqueues jobs when `REDIS_URL` is set. Run the worker like:

```
rq worker -u $REDIS_URL default
```

- Confirm the worker logs show job processing and no Redis auth errors.

S3 (optional) for outputs
-------------------------
- Configure AWS credentials via environment variables: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and optionally `AWS_REGION`.
- Set `S3_BUCKET` and optionally `S3_PREFIX` in the environment.
- When S3 is configured, the app will upload CSV and dashboard artifacts and return presigned URLs via the `/api/progress/<key>` endpoint.

Debugging tips
--------------
- Check web logs for lines like `Tesseract found:` which confirm tesseract binary is available.
- If progress stops at 100 but `done` is false, search logs for `Failed to write progress to Redis` or `Failed to finalize progress` — these are logged by the app when Redis writes fail.
- Confirm both services have the same `REDIS_URL`. If the worker is not running, enqueued jobs will sit in Redis but won't be processed.

Security
--------
- Limit uploads (app sets `MAX_CONTENT_LENGTH` by env var). Consider authentication for the validation endpoint in production.
- Use least-privilege IAM credentials for S3.

Maintenance
-----------
- Keep `tesseract` updated in Docker image builds.
- Monitor memory and disk because large PDFs and OCR may be memory/disk intensive.

Contact
-------
If you want, I can produce a Render `render.yaml` snippet and example Dockerfile adjustments tailored to your account.
