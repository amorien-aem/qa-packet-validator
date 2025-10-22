# QA Packet Validator - AI Development Guide

## Project Overview
Flask web app that validates QA packet PDFs for compliance with required fields, numerical ranges, and data consistency. Extracts text from PDFs using PyMuPDF with OCR fallback (Tesseract), generates CSV/Excel reports, and provides real-time progress tracking.

## Architecture & Data Flow

### Core Components
- **`app/app.py`**: Monolithic Flask app (~880 lines) - all routes, validation logic, progress tracking
- **`app/worker.py`**: Redis Queue (RQ) worker stub for background processing
- **`app/templates/index.html`**: Single-page UI with AJAX upload and progress polling
- **`validator.py`**: Legacy stub - actual validation in `app.py`

### Request Flow
1. **Upload**: PDF → `/api/validate` → save to `app/uploads/` → return `progressKey`
2. **Processing**: Sync validation OR Redis queue job (if `REDIS_URL` set)
3. **Extraction**: Page segmentation → text extraction → OCR fallback → field parsing  
4. **Validation**: Against 22 `REQUIRED_FIELDS` + `NUMERICAL_RANGES` (Resistance: 95-105, Dimension: 0.9-1.1)
5. **Output**: 3 CSV files in `app/exports/` (validation summary, field info, field pages)
6. **Delivery**: S3 upload (if configured) OR local `/download/<filename>` endpoint

### Progress Architecture
Three-tier fallback system:
- **Redis**: Production hash storage with `hset/hget` operations
- **JSON files**: Cross-process persistence in `app/progress/` directory
- **In-memory**: Thread-safe dict with locks as final fallback
- **Client**: 1000ms polling of `/api/progress/<key>` endpoint

## Critical Implementation Patterns

### PDF Field Extraction Strategy
`extract_fields()` uses positional parsing, not just regex:
```python
# 1. Find all field labels in lowercased text  
# 2. Sort by position, extract text between consecutive labels
# 3. Fallback to pre-compiled regex patterns if positional fails
# 4. Handle multi-line values and missing colons
```

### Environment-Driven Features
- **Local dev**: `python app/app.py` → port 3000, in-memory progress, local files
- **Production**: Gunicorn + Redis workers + S3 storage + presigned URLs
- **Feature detection**: `REDIS_URL`, `S3_BUCKET`, `S3_PREFIX` env vars enable optional services

### Lazy Import Pattern
Heavy dependencies (pandas, PyMuPDF, matplotlib) imported inside functions to reduce startup memory:
```python
def validate_pdf():
    import pandas as pd  # Only import when actually validating
```

## Development Workflows

### Local Development
```bash
python app/app.py                    # Dev server on :3000
pytest tests/                       # Minimal test suite
rq worker -u redis://localhost:6379/0  # Background worker (optional)
```

### Deployment Patterns
- **Render.com**: `render.yaml` → Docker build with Tesseract OCR
- **Heroku**: `Procfile` → Gunicorn web process  
- **SystemD**: `deploy/rq-worker.service` for production workers

### File System Layout
```
app/uploads/    # Temporary PDF uploads (user files)
app/exports/    # Generated CSV/Excel/PNG outputs  
app/progress/   # JSON progress files for worker visibility
```

## Critical Configuration

### Validation Constants (Hardcoded)
```python
REQUIRED_FIELDS = [
    "Customer Name", "Customer P.O. Number", "Customer Part Number",
    "Customer Part Number Revision", "AEM Part Number", "AEM Lot Number",
    # ... 22 total fields in specific order
]
NUMERICAL_RANGES = {
    "Resistance": (95, 105),    # Percentage bounds
    "Dimension": (0.9, 1.1)     # Tolerance bounds  
}
```

### Environment Variables
- `REDIS_URL`: Enables background job queues
- `S3_BUCKET`/`S3_PREFIX`: Enables cloud file storage
- `MAX_CONTENT_LENGTH`: Upload size limit (default 5MB)
- `PAGE_SEGMENT_SIZE`: Memory optimization for large PDFs (default 4)

### Dependencies & System Requirements
- **Tesseract OCR**: Must be on PATH, auto-detected at startup
- **Python packages**: Flask, pandas, PyMuPDF, pytesseract, redis, rq, boto3
- **OCR fallback**: Only runs when `page.get_text()` returns empty

## Testing & Debugging

### Current Test Coverage
Tests in `tests/` are minimal placeholders. Key areas needing coverage:
- Field extraction accuracy with real PDF samples
- Redis vs in-memory progress mode switching  
- S3 vs local file serving fallbacks
- OCR dependency mocking for CI

### Performance Characteristics
- **Memory**: Page segmentation prevents large PDF memory spikes
- **Speed**: Regex patterns pre-compiled, OCR only on text-extraction failure
- **Progress**: Updates every 10 pages to minimize I/O overhead
- **Concurrency**: Thread-safe progress updates, atomic JSON file operations