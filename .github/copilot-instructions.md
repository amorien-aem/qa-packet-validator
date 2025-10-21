# QA Packet Validator - AI Development Guide

## Project Overview
This is a Flask web app that validates QA packet PDFs for compliance with required fields, numerical ranges, and data consistency. It extracts text from PDFs using PyMuPDF with OCR fallback (Tesseract), generates CSV/Excel reports, and provides progress tracking.

## Architecture

### Core Components
- **`app/app.py`**: Main Flask app with all routes and validation logic (~650 lines)
- **`app/worker.py`**: Redis Queue (RQ) worker setup for background processing  
- **`validator.py`**: Legacy stub - actual validation is in `app.py`
- **`app/templates/index.html`**: Single-page UI with async progress tracking

### Data Flow
1. Upload PDF → `/api/validate` endpoint
2. Generate unique progress key, store file in `app/uploads/`
3. Either sync validation OR Redis queue job (if `REDIS_URL` set)
4. Extract fields from PDF pages using text + OCR fallback
5. Validate against `REQUIRED_FIELDS` list and `NUMERICAL_RANGES`
6. Generate 3 CSV outputs in `app/exports/`: validation summary, field info, field pages
7. Upload to S3 (if configured) or serve locally via `/download/<filename>`

### Progress Tracking
- **In-memory**: `progress_store` dict with threading locks
- **Persistent**: JSON files in `app/progress/` for cross-process visibility
- **Production**: Redis hash storage when `REDIS_URL` configured
- Client polls `/api/progress/<key>` every 1000ms

## Key Patterns

### PDF Field Extraction
The validation logic uses a sophisticated positional extraction approach in `extract_fields()`:
1. Find all field label positions in lowercased text
2. Extract value between current label and next label position
3. Fallback to regex patterns if positional fails
4. Handle multi-line values and missing colons

### Environment-Aware Deployment
- **Local**: In-memory progress, local file serving
- **Production**: Redis queues, S3 storage, presigned URLs
- Uses `REDIS_URL`, `S3_BUCKET`, `S3_PREFIX` env vars for feature toggling

### File Organization
- `app/uploads/`: Temporary uploaded files
- `app/exports/`: Generated CSV/Excel/PNG outputs  
- `app/progress/`: JSON progress files for cross-process sync

## Development Workflows

### Local Testing
```bash
python app/app.py  # Starts on port 3000
pytest tests/     # Basic route and function tests
```

### Worker Mode (with Redis)
```bash
rq worker -u redis://localhost:6379/0
```

### Required Dependencies
- **Tesseract OCR**: `apt-get install tesseract-ocr` (auto-detected on PATH)
- **Python**: pandas, PyMuPDF, pytesseract, Flask, openpyxl, matplotlib
- **Optional**: redis, rq, boto3 for production features

## Critical Implementation Details

### Validation Constants
- `REQUIRED_FIELDS`: 22 hardcoded field names in specific order
- `NUMERICAL_RANGES`: Resistance (95-105), Dimension (0.9-1.1)
- OCR only runs if `page.get_text()` returns empty

### Error Handling
- Graceful Redis/S3 failures with local fallbacks
- File size limits via `MAX_CONTENT_LENGTH` (default 10MB)
- Comprehensive logging with structured JSON progress updates

### Performance Considerations  
- Regex patterns pre-compiled for all fields
- OCR at 150 DPI only when text extraction fails
- Progress updates every 10 pages to avoid overhead
- Atomic file operations for progress persistence

## Testing Strategy
Tests are minimal placeholders in `tests/`. When adding features:
- Test both Redis and in-memory progress modes
- Verify S3 vs local file serving paths  
- Mock OCR dependencies for CI environments
- Test field extraction with real PDF samples in `app/exports/`