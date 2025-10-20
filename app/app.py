from flask import session
import threading
import time

# In-memory progress store (for demo; use Redis or DB for production)
progress_store = {}
progress_store_lock = threading.Lock()

import os
from flask import Flask, request, send_from_directory, jsonify, render_template_string
from werkzeug.utils import secure_filename
import pandas as pd
import fitz  # PyMuPDF
import csv
import re
import matplotlib.pyplot as plt
from collections import defaultdict
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.worksheet.table import Table, TableStyleInfo
import pytesseract
from PIL import Image
import io
import uuid
import shutil
import traceback
import subprocess
import logging
import platform
import sys
import psutil
import boto3
from botocore.exceptions import ClientError
from urllib.parse import urlparse
try:
    from redis import Redis
    from rq import Queue
except Exception:
    Redis = None
    Queue = None

# Use absolute paths for directories to avoid issues in cloud environments
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
EXPORTS_FOLDER = os.path.join(BASE_DIR, 'exports')
ALLOWED_EXTENSIONS = {'pdf', 'csv', 'xlsx'}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(EXPORTS_FOLDER, exist_ok=True)

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['EXPORTS_FOLDER'] = EXPORTS_FOLDER
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_CONTENT_LENGTH', 10 * 1024 * 1024))  # 10MB default

# Structured logging and optional Sentry
SENTRY_DSN = os.environ.get('SENTRY_DSN')
JSON_LOGS = os.environ.get('JSON_LOGS', '1') in ('1', 'true', 'True')

# Try to initialize Sentry if DSN provided
try:
    if SENTRY_DSN:
        import sentry_sdk

        sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=float(os.environ.get('SENTRY_TRACES_SAMPLE_RATE', '0.0')))
except Exception:
    # don't fail startup just because Sentry isn't available
    pass

# Configure logging
if JSON_LOGS:
    try:
        from pythonjsonlogger import jsonlogger

        handler = logging.StreamHandler()
        fmt = jsonlogger.JsonFormatter('%(asctime)s %(levelname)s %(name)s %(message)s')
        handler.setFormatter(fmt)
        logger = logging.getLogger('qa-validator')
        logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO'))
        # Remove default handlers then add JSON handler
        logging.root.handlers = []
        logging.root.addHandler(handler)
    except Exception:
        logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'))
        logger = logging.getLogger('qa-validator')
else:
    logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'))
    logger = logging.getLogger('qa-validator')

# Try to configure Redis if provided (Render/Production)
REDIS_URL = os.environ.get('REDIS_URL')
redis_conn = None
rq_queue = None
if REDIS_URL:
    try:
        redis_conn = Redis.from_url(REDIS_URL)
        rq_queue = Queue('default', connection=redis_conn)
        logger.info('Connected to Redis for background jobs')
    except Exception as e:
        logger.exception('Failed to connect to Redis: %s', e)

# Progress key prefix and TTL (days)
PROGRESS_KEY_PREFIX = os.environ.get('PROGRESS_KEY_PREFIX', 'qa-validator:progress:')
_PROGRESS_TTL_DAYS = int(os.environ.get('PROGRESS_KEY_TTL_DAYS', '7'))
PROGRESS_KEY_TTL_SECONDS = _PROGRESS_TTL_DAYS * 24 * 3600


def set_progress(progress_key, percent=None, csv_filename=None, done=None):
    """Set progress in Redis if configured, otherwise in the in-memory store."""
    # Use namespaced key internally to avoid collisions
    namespaced_key = f"{PROGRESS_KEY_PREFIX}{progress_key}"
    if redis_conn:
        data = {}
        if percent is not None:
            data['percent'] = int(percent)
        if csv_filename is not None:
            data['csv_filename'] = csv_filename
        if done is not None:
            data['done'] = int(bool(done))
        if data:
            # Try a small retry/backoff to handle transient Redis errors before falling back
            max_attempts = 3
            attempt = 0
            last_exc = None
            payload = {k: str(v) for k, v in data.items()}
            while attempt < max_attempts:
                try:
                    redis_conn.hset(namespaced_key, mapping=payload)
                    # If marking done, set a TTL so keys don't grow indefinitely
                    if redis_conn:
                        data = {}
                        if percent is not None:
                            data['percent'] = int(percent)
                        if csv_filename is not None:
                            data['csv_filename'] = csv_filename
                        if done is not None:
                            data['done'] = int(bool(done))
                        if data:
                            # Try a few times with exponential backoff for transient Redis errors
                            attempts = 3
                            backoff_base = 0.25
                            for attempt in range(1, attempts + 1):
                                try:
                                    redis_conn.hset(namespaced_key, mapping={k: str(v) for k, v in data.items()})
                                    # If marking done, set a TTL so keys don't grow indefinitely
                                    if done:
                                        try:
                                            redis_conn.expire(namespaced_key, PROGRESS_KEY_TTL_SECONDS)
                                        except Exception:
                                            logger.exception('Failed to set TTL on progress key %s', namespaced_key)
                                    return
                                except Exception:
                                    logger.exception('Attempt %s: Failed to write progress to Redis for key %s', attempt, progress_key)
                                    if attempt < attempts:
                                        time.sleep(backoff_base * (2 ** (attempt - 1)))
                                    else:
                                        logger.warning('All attempts failed; falling back to in-memory progress for key %s', progress_key)
                                        # fall through to memory fallback
            prog['percent'] = int(percent)
        if csv_filename is not None:
            prog['csv_filename'] = csv_filename
        if done is not None:
            prog['done'] = bool(done)


def get_progress_data(progress_key):
    """Retrieve progress dict from Redis or memory-alike structure."""
    namespaced_key = f"{PROGRESS_KEY_PREFIX}{progress_key}"
    if redis_conn:
        try:
            h = redis_conn.hgetall(namespaced_key)
            if not h:
                return {'percent': 0, 'done': False, 'csv_filename': None}
            # decode bytes to str
            decoded = {k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v for k, v in h.items()}
            return {
                'percent': int(decoded.get('percent', 0)),
                'done': bool(int(decoded.get('done', 0))) if decoded.get('done') is not None else False,
                'csv_filename': decoded.get('csv_filename')
            }
        except Exception:
            logger.exception('Error reading progress from Redis for key %s', progress_key)
            return {'percent': 0, 'done': False, 'csv_filename': None}
    else:
        with progress_store_lock:
            return progress_store.get(progress_key, {'percent': 0, 'done': False, 'csv_filename': None})


@app.route('/api/redis_ping', methods=['GET'])
def redis_ping():
    """Return masked Redis host:port and whether a ping succeeds. Does not reveal credentials."""
    if not REDIS_URL or not redis_conn:
        return jsonify({'redis_configured': False, 'host': None, 'port': None, 'reachable': False})
    try:
        parsed = urlparse(REDIS_URL)
        host = parsed.hostname
        port = parsed.port
    except Exception:
        host = None
        port = None
    reachable = False
    try:
        reachable = bool(redis_conn.ping())
    except Exception:
        logger.exception('Redis ping failed')
        reachable = False
    return jsonify({'redis_configured': True, 'host': host, 'port': port, 'reachable': reachable})


def upload_to_s3(local_path, bucket, key_prefix=''):
    """Upload file to S3 and return the object key."""
    s3 = boto3.client('s3')
    key = os.path.join(key_prefix, os.path.basename(local_path)) if key_prefix else os.path.basename(local_path)
    try:
        s3.upload_file(local_path, bucket, key)
        return key
    except ClientError as e:
        logger.exception('S3 upload failed: %s', e)
        return None


def presigned_url(bucket, key, expires=3600):
    s3 = boto3.client('s3')
    try:
        url = s3.generate_presigned_url('get_object', Params={'Bucket': bucket, 'Key': key}, ExpiresIn=expires)
        return url
    except ClientError as e:
        logger.exception('Presigned URL generation failed: %s', e)
        return None

# Ensure pytesseract knows the tesseract binary location in deployed environments
tess_path = shutil.which('tesseract')
if tess_path:
    try:
        pytesseract.pytesseract.tesseract_cmd = tess_path
        ver = subprocess.check_output([tess_path, '--version'], stderr=subprocess.STDOUT, text=True).splitlines()[0]
        logger.info("Tesseract found: %s (%s)", tess_path, ver)
    except Exception as e:
        logger.exception("Tesseract found at %s but failed to run --version: %s", tess_path, e)
else:
    logger.warning("Tesseract binary not found on PATH; OCR fallback may fail on this host.")

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_text_with_ocr(page):
    text = page.get_text()
    if text.strip():
        return text
    # Fallback to OCR if no text found
    pix = page.get_pixmap(dpi=150)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    text = pytesseract.image_to_string(img)
    img.close()
    return text


def clean_output(s: str) -> str:
    """Sanitize and normalize extracted output for CSV readability."""
    if s is None:
        return ''
    # Collapse multiple spaces, normalize whitespace and trim
    out = re.sub(r"\s+", " ", str(s)).strip()
    # Remove repeated 'Checked' markers or long runs
    out = re.sub(r"(Checked\s*\d{0,3}\.?\s*){2,}", "Checked", out, flags=re.IGNORECASE)
    return out[:4000]

def validate_pdf(pdf_path, export_dir, progress_key=None, result_key=None):
    base_name = os.path.splitext(os.path.basename(pdf_path))[0]
    csv_path = os.path.join(export_dir, f"{base_name}_validation_summary.csv")
    excel_path = os.path.join(export_dir, f"{base_name}_validation_summary.xlsx")
    dashboard_path = os.path.join(export_dir, f"{base_name}_dashboard.png")

    REQUIRED_FIELDS = [
        "Customer Name", "Customer P.O. Number", "Customer Part Number",
        "Customer Part Number Revision", "AEM Part Number", "AEM Lot Number",
        "AEM Date Code", "AEM Cage Code", "Customer Quality Clauses",
        "FAI Form 3", "Solderability Test Report", "DPA", "Visual Inspection Record",
        "Shipment Quantity", "Reel Labels", "Certificate of Conformance", "Route Sheet",
        "Part Number", "Lot Number", "Date", "Resistance", "Dimension", "Test Result"
    ]

    # Precompile regex patterns for all fields for speed
    FIELD_PATTERNS = {field: re.compile(rf"{re.escape(field)}[:\s]*([^\n]+)", re.IGNORECASE) for field in REQUIRED_FIELDS}

    NUMERICAL_RANGES = {
        "Resistance": (95, 105),
        "Dimension": (0.9, 1.1)
    }

    anomalies = []
    critical_issues = []
    field_presence = defaultdict(int)
    all_fields = []
    field_page_map = defaultdict(list)  # field -> list of page numbers
    field_value_map = defaultdict(list)  # field -> list of (page, value)

    def extract_fields(text):
        """Extract fields by locating field label positions and taking the text up to the next label.
        This captures multi-line values and cases where a colon isn't present.
        Falls back to the simple regex if positional extraction doesn't find a value.
        """
        fields = {}
        if not text:
            return fields

        # Work with the original text for extraction but perform case-insensitive searches
        lower_text = text.lower()

        # Find all label occurrences with their span
        occurrences = []  # list of (start_index, end_index, field)
        for field in REQUIRED_FIELDS:
            low_field = field.lower()
            for m in re.finditer(re.escape(low_field), lower_text):
                occurrences.append((m.start(), m.end(), field))

        # If no positional occurrences found, fall back to simple pattern matching
        if not occurrences:
            for field, pattern in FIELD_PATTERNS.items():
                match = pattern.search(text)
                if match:
                    fields[field] = match.group(1).strip()
            return fields

        # Sort occurrences by position
        occurrences.sort(key=lambda x: x[0])

        for idx, (start, end, field) in enumerate(occurrences):
            value_start = end
            value_end = occurrences[idx + 1][0] if idx + 1 < len(occurrences) else len(text)
            raw_value = text[value_start:value_end]
            # Remove leading separators and whitespace
            raw_value = re.sub(r"^[\s:.-]*", "", raw_value)
            # Trim trailing whitespace/newlines and collapse internal whitespace
            value = re.sub(r"\s+", " ", raw_value).strip()
            # Limit to a reasonable length to avoid grabbing huge blocks
            if value:
                fields[field] = value[:1000]

        # For any required field still missing, try the regex fallback once
        for field, pattern in FIELD_PATTERNS.items():
            if field not in fields:
                match = pattern.search(text)
                if match:
                    fields[field] = match.group(1).strip()

        return fields

    def validate_numerical(field, value):
        try:
            val = float(re.findall(r"[\d.]+", value)[0])
            min_val, max_val = NUMERICAL_RANGES[field]
            return min_val <= val <= max_val
        except:
            return False

    def check_consistency(field_name):
        values = [fields.get(field_name) for fields in all_fields if field_name in fields]
        return len(set(values)) == 1

    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    for page_num in range(total_pages):
        page = doc.load_page(page_num)
        # Try to extract text directly; only use OCR if text is empty
        text = page.get_text()
        if not text.strip():
            # Only do OCR if absolutely necessary
            pix = page.get_pixmap(dpi=150)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            try:
                text = pytesseract.image_to_string(img)
            finally:
                img.close()
        fields = extract_fields(text)
        all_fields.append(fields)

        for field in REQUIRED_FIELDS:
            if field not in fields:
                anomalies.append([page_num + 1, field, "Missing"])
            else:
                field_presence[field] += 1
                field_page_map[field].append(page_num + 1)
                field_value_map[field].append((page_num + 1, fields[field]))

        for field in NUMERICAL_RANGES:
            if field in fields and not validate_numerical(field, fields[field]):
                anomalies.append([page_num + 1, field, f"Out of range: {fields[field]}"])
                critical_issues.append([page_num + 1, field, fields[field]])

        # Update progress
        if progress_key:
            set_progress(progress_key, percent=int(((page_num + 1) / total_pages) * 100))
        # Log progress at a coarse level
        if (page_num + 1) % 10 == 0 or page_num == total_pages - 1:
            logger.info('Processed page %s/%s for %s', page_num + 1, total_pages, base_name)

    for field in ["Part Number", "Lot Number", "Date"]:
        if not check_consistency(field):
            anomalies.append(["All Pages", field, "Inconsistent values"])
            critical_issues.append(["All Pages", field, "Inconsistent values"])

    # Write all required fields for every page: Page, Field, Result ('Found' or 'Missing'), Output (value or blank)
    with open(csv_path, "w", newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["Page", "Field", "Result", "Output"])
        for page_num in range(1, total_pages + 1):
            # Extract fields for this page
            fields = all_fields[page_num - 1]
            for field in REQUIRED_FIELDS:
                if field in fields:
                    writer.writerow([page_num, field, "Found", fields[field]])
                else:
                    writer.writerow([page_num, field, "Missing", ""])

    # Write summary: all required fields, 'Found' if present, and all extracted values (concatenated)
    field_info_csv = os.path.join(export_dir, f"{base_name}_field_info_summary.csv")
    with open(field_info_csv, "w", newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["Field", "Status", "Output"])
        for field in REQUIRED_FIELDS:
            values = field_value_map[field]
            if values:
                # Concatenate all extracted values for this field, preserving all characters
                value_str = '; '.join(v for _, v in values)
                writer.writerow([field, "Found", value_str])
            else:
                writer.writerow([field, "Not found", "Not found"])

    wb = Workbook()
    ws = wb.active
    ws.title = "QA Anomalies"

    headers = ["Page", "Field", "Issue"]
    for col_num, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_num, value=header)
        cell.font = Font(bold=True)

    for row_num, row_data in enumerate(anomalies, start=2):
        for col_num, cell_value in enumerate(row_data, start=1):
            ws.cell(row=row_num, column=col_num, value=cell_value)

    table_ref = f"A1:C{len(anomalies)+1}"
    table = Table(displayName="AnomalyTable", ref=table_ref)
    style = TableStyleInfo(name="TableStyleMedium9", showFirstColumn=False,
                           showLastColumn=False, showRowStripes=True, showColumnStripes=False)
    table.tableStyleInfo = style
    ws.add_table(table)

    for col in ws.columns:
        max_length = max(len(str(cell.value)) if cell.value else 0 for cell in col)
        ws.column_dimensions[col[0].column_letter].width = max_length + 2

    wb.save(excel_path)

    plt.figure(figsize=(12, 6))
    plt.bar(field_presence.keys(), field_presence.values(), color='skyblue')
    plt.title("Field Presence Across PDF Pages")
    plt.xlabel("Field Name")
    plt.ylabel("Number of Pages Present")
    plt.xticks(rotation=90)
    plt.tight_layout()
    plt.savefig(dashboard_path)
    # If S3 configured, upload CSV and dashboard, and return presigned URL through progress store
    s3_bucket = os.environ.get('S3_BUCKET')
    s3_prefix = os.environ.get('S3_PREFIX', '')
    if s3_bucket:
        csv_key = upload_to_s3(csv_path, s3_bucket, s3_prefix)
        dash_key = upload_to_s3(dashboard_path, s3_bucket, s3_prefix)
        field_info_key = upload_to_s3(field_info_csv, s3_bucket, s3_prefix)
        if progress_key and result_key:
            # store the object key as csv_filename so the API can return a presigned URL
            set_progress(progress_key, percent=100, csv_filename=os.path.basename(csv_key) if csv_key else None, done=True)
    else:
        # Save result in progress store for robust retrieval
        if progress_key and result_key:
            set_progress(progress_key, percent=100, csv_filename=os.path.basename(csv_path), done=True)

    logger.info('Validation complete. CSV saved at %s', csv_path)
    return csv_path, excel_path, dashboard_path, len(anomalies), len(critical_issues)

def validate_file(filepath, progress_key=None, result_key=None):
    # If PDF, run PDF validation, else fallback to dummy
    if filepath.lower().endswith('.pdf'):
        df = None
        csv_path, excel_path, dashboard_path, anomaly_count, critical_count = validate_pdf(filepath, EXPORTS_FOLDER, progress_key, result_key)
        df = pd.read_csv(csv_path)
        csv_filename = os.path.basename(csv_path)
        print(f"validate_file: CSV generated at {csv_path}")
        # Defensive: ensure progress finalized when called directly (validate_pdf should already set this)
        if progress_key and result_key:
            try:
                set_progress(progress_key, percent=100, csv_filename=csv_filename, done=True)
            except Exception:
                logger.exception('Failed to set final progress in validate_file for key %s', progress_key)
        return df, csv_filename
    else:
        data = {'filename': [os.path.basename(filepath)], 'status': ['validated']}
        df = pd.DataFrame(data)
        csv_filename = os.path.splitext(os.path.basename(filepath))[0] + '.csv'
        csv_path = os.path.join(EXPORTS_FOLDER, csv_filename)
        df.to_csv(csv_path, index=False)
        if progress_key and result_key:
            set_progress(progress_key, percent=100, csv_filename=csv_filename, done=True)
        print(f"validate_file: Non-PDF CSV generated at {csv_path}")
        return df, csv_filename

def export_to_csv(df, csv_path):
    df.to_csv(csv_path, index=False)

@app.route('/', methods=['GET'])
def index():
    return render_template_string('''
    <h2>Upload file for validation</h2>
    <p>1. Select a file.<br>
    2. Click <b>Upload and Validate</b>.<br>
    3. Wait for both progress bars to reach 100%.<br>
    4. When validation is complete, click the <b>Download CSV</b> link.</p>
    <form id="upload-form" method="post" action="/api/validate" enctype="multipart/form-data">
      <input type="file" name="file" id="file-input">
      <input type="submit" value="Upload and Validate">
    </form>
    <div style="margin-top:20px;">
      <div>Upload Progress: <span id="upload-percent">0%</span></div>
      <div id="upload-progress-bar" style="width: 100%; background: #eee; height: 20px;">
        <div id="upload-progress" style="background: #2196f3; width: 0%; height: 100%;"></div>
      </div>
    </div>
    <div style="margin-top:20px;">
      <div>Validation Progress: <span id="progress-percent">0%</span></div>
      <div id="progress-bar" style="width: 100%; background: #eee; height: 20px;">
        <div id="progress" style="background: #4caf50; width: 0%; height: 100%;"></div>
      </div>
    </div>
            <div id="download-link" style="margin-top:20px;"></div>
            <div id="csv-preview" style="margin-top:12px; display:none; border:1px solid #ddd; padding:8px; background:#fafafa; max-width:900px;">
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <strong id="csv-preview-filename"></strong>
                    <button id="csv-preview-close" style="background:#eee;border:1px solid #ccc;padding:4px 8px;border-radius:4px;">Close</button>
                </div>
                <pre id="csv-preview-content" style="max-height:320px;overflow:auto;white-space:pre-wrap;word-break:break-word;margin-top:8px;"></pre>
            </div>
            <div id="toast" style="position:fixed;right:20px;bottom:20px;min-width:200px;background:#323232;color:#fff;padding:12px;border-radius:6px;display:none;box-shadow:0 2px 10px rgba(0,0,0,0.3);z-index:1000;"></div>
            <script>
            function showToast(msg, timeout=4000) {
                const t = document.getElementById('toast');
                t.innerText = msg;
                t.style.display = 'block';
                t.style.opacity = '1';
                setTimeout(() => {
                    t.style.transition = 'opacity 0.5s';
                    t.style.opacity = '0';
                    setTimeout(()=> t.style.display = 'none', 500);
                }, timeout);
            }

            async function showPreview(csvFilename) {
                const previewDiv = document.getElementById('csv-preview');
                const content = document.getElementById('csv-preview-content');
                const name = document.getElementById('csv-preview-filename');
                previewDiv.style.display = 'block';
                name.innerText = csvFilename;
                content.innerText = 'Loading preview...';
                try {
                    let res = await fetch(`/download/${encodeURIComponent(csvFilename)}`, { method: 'GET' });
                    const ctype = res.headers.get('content-type') || '';
                    let text;
                    if (ctype.includes('application/json')) {
                        const j = await res.json();
                        if (j.url) {
                            const r2 = await fetch(j.url);
                            if (!r2.ok) throw new Error('Failed to fetch presigned URL: ' + r2.status);
                            text = await r2.text();
                        } else {
                            text = JSON.stringify(j, null, 2);
                        }
                    } else {
                        if (!res.ok) throw new Error('Failed to download CSV: ' + res.status);
                        text = await res.text();
                    }
                    const lines = text.split(/\r?\n/).slice(0, 80);
                    content.innerText = lines.join('\n');
                } catch (err) {
                    content.innerText = 'Preview failed: ' + String(err);
                }
            }

            document.getElementById('csv-preview-close').addEventListener('click', function(){
                document.getElementById('csv-preview').style.display = 'none';
            });

            document.getElementById('upload-form').onsubmit = async function(e) {
      e.preventDefault();
      const formData = new FormData(this);
      let uploadProgressBar = document.getElementById('upload-progress');
      let uploadPercentText = document.getElementById('upload-percent');
      let progressBar = document.getElementById('progress');
      let progressPercentText = document.getElementById('progress-percent');
      uploadProgressBar.style.width = '0%';
      uploadPercentText.innerText = '0%';
      progressBar.style.width = '0%';
      progressPercentText.innerText = '0%';
      document.getElementById('download-link').innerHTML = '';

      // AJAX upload with progress
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/validate', true);

      xhr.upload.onprogress = function(e) {
        if (e.lengthComputable) {
          let percent = Math.round((e.loaded / e.total) * 100);
          uploadProgressBar.style.width = percent + '%';
          uploadPercentText.innerText = percent + '%';
        }
      };

            xhr.onreadystatechange = async function() {
                if (xhr.readyState === XMLHttpRequest.DONE) {
                    if (xhr.status === 200) {
                        uploadProgressBar.style.width = '100%';
                        uploadPercentText.innerText = '100%';
                        const data = JSON.parse(xhr.responseText);
                        // If server returned an immediate csv_filename (synchronous fallback when Redis absent), show download link
                                    if (data.csv_filename) {
                                        const csvFilename = data.csv_filename;
                                        const downloadUrl = data.download_url ? data.download_url : `/download/${csvFilename}`;
                                        progressBar.style.width = '100%';
                                        progressPercentText.innerText = '100%';
                                        document.getElementById('download-link').innerHTML =
                                            `<b>Validation complete! Click the link below to download your results:</b><br>
                                            <a href="${downloadUrl}" download>Download CSV</a> <button id="preview-btn" style="margin-left:8px;padding:4px 8px;border-radius:4px;border:1px solid #ccc;background:#f5f5f5;">Preview CSV</button>`;
                                        setTimeout(()=>{
                                            const btn = document.getElementById('preview-btn');
                                            if (btn) btn.addEventListener('click', () => showPreview(csvFilename));
                                        }, 10);
                                        showToast('Validation complete — download is ready');
                                        return;
                                    }
                        if (!data.progressKey) {
                            document.getElementById('download-link').innerText = 'Validation failed.';
                            showToast('Validation failed', 6000);
                            return;
                        }
                        // Poll for validation progress
                        let percent = 0;
                        let csvFilename = '';
                        while (percent < 100) {
                            const progRes = await fetch(`/api/progress/${data.progressKey}`);
                            const progData = await progRes.json();
                            percent = progData.percent;
                            progressBar.style.width = percent + '%';
                            progressPercentText.innerText = percent + '%';
                            if (progData.done && progData.csv_filename) {
                                csvFilename = progData.csv_filename;
                                break;
                            }
                            await new Promise(r => setTimeout(r, 1000));
                        }
                        progressBar.style.width = '100%';
                        progressPercentText.innerText = '100%';
                        if (csvFilename) {
                            document.getElementById('download-link').innerHTML =
                                `<b>Validation complete! Click the link below to download your results:</b><br>
                                <a href="/download/${csvFilename}" download>Download CSV</a> <button id="preview-btn" style="margin-left:8px;padding:4px 8px;border-radius:4px;border:1px solid #ccc;background:#f5f5f5;">Preview CSV</button>`;
                            setTimeout(()=>{
                              const btn = document.getElementById('preview-btn');
                              if (btn) btn.addEventListener('click', () => showPreview(csvFilename));
                            }, 10);
                            showToast('Validation complete — download is ready');
                        } else {
                            document.getElementById('download-link').innerText = 'Validation failed.';
                            showToast('Validation failed', 6000);
                        }
                    } else {
                        document.getElementById('download-link').innerText = 'Upload failed.';
                        showToast('Upload failed', 6000);
                    }
                }
            };
      xhr.send(formData);
    }
    </script>
    ''')

@app.route('/api/validate', methods=['POST'])
def api_validate():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        upload_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(upload_path)

        # Generate a unique progress key
        progress_key = str(uuid.uuid4())
        set_progress(progress_key, percent=0, csv_filename=None, done=False)
        def run_validation_local(progress_key, upload_path, filename):
            # Run validation but guarantee final progress write in a finally block
            csv_filename = None
            try:
                print(f"Starting validation for {upload_path}")
                csv_path, excel_path, dashboard_path, anomaly_count, critical_count = validate_pdf(upload_path, EXPORTS_FOLDER, progress_key, progress_key)
                csv_filename = os.path.basename(csv_path)
                print(f"Validation finished for {upload_path}, CSV: {csv_filename}")
            except Exception as e:
                error_csv = os.path.splitext(filename)[0] + "_validation_summary.csv"
                error_csv_path = os.path.join(EXPORTS_FOLDER, error_csv)
                try:
                    tb = traceback.format_exc()
                    with open(error_csv_path, "w", newline='') as f:
                        writer = csv.writer(f)
                        writer.writerow(["Error"])
                        writer.writerow([str(e)])
                        writer.writerow(["Traceback:"])
                        for line in tb.splitlines():
                            writer.writerow([line])
                    csv_filename = error_csv
                except Exception as file_error:
                    print(f"Error writing error CSV: {file_error}")
                    csv_filename = None
                print(f"Validation error: {e}")
            finally:
                # Ensure the progress store is finalized so callers/pollers always see a completed state
                if progress_key:
                    try:
                        set_progress(progress_key, percent=100, csv_filename=csv_filename, done=True)
                    except Exception:
                        logger.exception('Failed to finalize progress in run_validation_local for key %s', progress_key)
        # If Redis is not configured, run synchronously and return immediate result
        if not redis_conn:
            try:
                df, csv_filename = validate_file(upload_path, progress_key=progress_key, result_key=progress_key)
                # progress already finalized by validate_file
                return jsonify({'progressKey': progress_key, 'csv_filename': csv_filename})
            except Exception as e:
                logger.exception('Synchronous validation failed for %s', upload_path)
                return jsonify({'error': str(e)}), 500

        # Otherwise, try to enqueue the validation job to Redis queue
        if rq_queue:
            try:
                job = rq_queue.enqueue('app.validate_file', upload_path, progress_key, progress_key)
                print('Enqueued job', job.id)
            except Exception as e:
                print('Failed to enqueue job, falling back to thread:', e)
                thread = threading.Thread(target=run_validation_local, args=(progress_key, upload_path, filename))
                thread.start()
        else:
            # Run locally in background thread
            thread = threading.Thread(target=run_validation_local, args=(progress_key, upload_path, filename))
            thread.start()

        return jsonify({'progressKey': progress_key})
    return jsonify({'error': 'Invalid file type'}), 400

@app.route('/api/progress/<progress_key>', methods=['GET'])
def get_progress(progress_key):
    prog = get_progress_data(progress_key)
    logger.info('Progress check for key %s: %s', progress_key, prog)
    if not prog:
        return jsonify({'percent': 0, 'done': False, 'csv_filename': None, 'error': 'Progress key not found'}), 404

    # If S3 is configured and csv_filename is present, return presigned URL
    s3_bucket = os.environ.get('S3_BUCKET')
    if s3_bucket and prog.get('csv_filename'):
        s3_prefix = os.environ.get('S3_PREFIX', '')
        key = os.path.join(s3_prefix, prog.get('csv_filename')) if s3_prefix else prog.get('csv_filename')
        url = presigned_url(s3_bucket, key)
        return jsonify({
            'percent': prog.get('percent', 0),
            'done': prog.get('done', False),
            'csv_filename': prog.get('csv_filename'),
            'download_url': url
        })

    return jsonify({
        'percent': prog.get('percent', 0),
        'done': prog.get('done', False),
        'csv_filename': prog.get('csv_filename')
    })

@app.route('/download/<csv_filename>', methods=['GET'])
def download_csv(csv_filename):
    # If S3 is configured, return presigned URL; otherwise serve from exports folder
    s3_bucket = os.environ.get('S3_BUCKET')
    s3_prefix = os.environ.get('S3_PREFIX', '')
    if s3_bucket:
        key = os.path.join(s3_prefix, csv_filename) if s3_prefix else csv_filename
        url = presigned_url(s3_bucket, key)
        if url:
            return jsonify({'url': url})
        else:
            return "File not available", 404
    try:
        logger.info('Download requested for %s', csv_filename)
        return send_from_directory(app.config['EXPORTS_FOLDER'], csv_filename, as_attachment=True)
    except FileNotFoundError:
        logger.warning('File not found for download: %s', csv_filename)
        return "File not found", 404


@app.route('/api/diagnostics', methods=['GET'])
def diagnostics():
    info = {
        'python': sys.version.splitlines()[0],
        'platform': platform.platform(),
        'tesseract': getattr(pytesseract.pytesseract, 'tesseract_cmd', None),
        'redis': bool(REDIS_URL),
        's3_bucket': os.environ.get('S3_BUCKET'),
        'memory_mb': psutil.virtual_memory().total // (1024 * 1024),
        'disk_free_mb': shutil.disk_usage(BASE_DIR).free // (1024 * 1024)
    }
    return jsonify(info)


@app.route('/api/health', methods=['GET'])
def health():
    """Return quick health info: redis reachability, redis url masked, and RQ queue size if available."""
    info = {'ok': True}
    # Redis info
    info['redis_configured'] = bool(REDIS_URL)
    info['redis_reachable'] = False
    if REDIS_URL and redis_conn:
        try:
            info['redis_reachable'] = bool(redis_conn.ping())
        except Exception:
            info['redis_reachable'] = False
    # RQ info (approximate queue length)
    try:
        if rq_queue and redis_conn:
            # RQ doesn't expose length directly; inspect the Redis list for simplicity
            q_name = rq_queue.name
            q_key = f"rq:queue:{q_name}"
            try:
                info['queue_length'] = redis_conn.llen(q_key)
            except Exception:
                info['queue_length'] = None
        else:
            info['queue_length'] = None
    except Exception:
        info['queue_length'] = None

    return jsonify(info)


@app.route('/api/health', methods=['GET'])
def health():
    """Lightweight health check useful for load balancers and quick status checks.
    Returns Redis reachability, queue length (if Redis/RQ available), and a simple ok flag.
    """
    ok = True
    redis_status = {'configured': False, 'reachable': False, 'host': None, 'port': None}
    queue_len = None
    try:
        if REDIS_URL and redis_conn:
            redis_status['configured'] = True
            parsed = urlparse(REDIS_URL)
            redis_status['host'] = parsed.hostname
            redis_status['port'] = parsed.port
            try:
                redis_status['reachable'] = bool(redis_conn.ping())
            except Exception:
                redis_status['reachable'] = False
            # If rq is available, attempt to get queue length
            if Queue and redis_conn:
                try:
                    q = Queue('default', connection=redis_conn)
                    queue_len = q.count
                except Exception:
                    logger.exception('Failed to read RQ queue length')
    except Exception:
        logger.exception('Health check failed')
        ok = False

    return jsonify({
        'ok': ok,
        'redis': redis_status,
        'queue_length': queue_len
    })


@app.route('/api/debug_job/<progress_key>', methods=['GET'])
def debug_job(progress_key):
    """Return detailed debug info for a job/progress key.
    - reads progress store
    - if csv_filename present, checks local exports folder for the file and returns file stat + head
    - if S3 configured and csv present, returns a presigned URL check
    This endpoint is intended for debugging and should be removed/revoked in production if not needed.
    """
    prog = get_progress_data(progress_key)
    resp = {'progress': prog}

    csv_filename = prog.get('csv_filename')
    s3_bucket = os.environ.get('S3_BUCKET')

    if csv_filename:
        local_path = os.path.join(app.config['EXPORTS_FOLDER'], csv_filename)
        resp['local_export'] = {'exists': False}
        try:
            if os.path.exists(local_path):
                st = os.stat(local_path)
                resp['local_export']['exists'] = True
                resp['local_export']['size'] = st.st_size
                # return first 2000 chars or 80 lines
                head_lines = []
                with open(local_path, 'r', encoding='utf-8', errors='replace') as f:
                    for i, line in enumerate(f):
                        if i >= 80:
                            break
                        head_lines.append(line.rstrip('\n'))
                resp['local_export']['preview_lines'] = head_lines
        except Exception as e:
            resp['local_export']['error'] = str(e)

        # If S3 configured, generate a presigned URL (masked) and test GET
        if s3_bucket:
            s3_prefix = os.environ.get('S3_PREFIX', '')
            key = os.path.join(s3_prefix, csv_filename) if s3_prefix else csv_filename
            url = presigned_url(s3_bucket, key)
            resp['s3'] = {'presigned_url': bool(url)}
            # Optionally test fetch (server-side) but don't include content
            if url:
                try:
                    import requests

                    r = requests.get(url, timeout=15)
                    resp['s3']['status_code'] = r.status_code
                except Exception as e:
                    resp['s3']['fetch_error'] = str(e)
    else:
        resp['note'] = 'No csv_filename present in progress entry.'

    return jsonify(resp)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 3000))
    app.run(host='0.0.0.0', port=port)