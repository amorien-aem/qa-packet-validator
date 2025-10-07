from flask import session
import threading
import time

# In-memory progress store (for demo; use Redis or DB for production)
progress_store = {}

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

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
EXPORTS_FOLDER = os.path.join(os.path.dirname(__file__), 'exports')
ALLOWED_EXTENSIONS = {'pdf', 'csv', 'xlsx'}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(EXPORTS_FOLDER, exist_ok=True)

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['EXPORTS_FOLDER'] = EXPORTS_FOLDER

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_text_with_ocr(page):
    text = page.get_text()
    if text.strip():
        return text
    # Fallback to OCR if no text found
    pix = page.get_pixmap(dpi=300)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    text = pytesseract.image_to_string(img)
    return text

def validate_pdf(pdf_path, export_dir, progress_key=None):
    base_name = os.path.splitext(os.path.basename(pdf_path))[0]
    csv_path = os.path.join(export_dir, f"{base_name}_validation_summary.csv")
    excel_path = os.path.join(export_dir, f"{base_name}_validation_summary.xlsx")
    dashboard_path = os.path.join(export_dir, f"{base_name}_dashboard.png")

    REQUIRED_FIELDS = [
        "Customer Name", "Customer P.O. Number", "Customer Part Number",
        "Customer Part Number Revision", "OEM Part Number", "OEM Lot Number",
        "OEM Date Code", "OEM Cage Code", "AEM Part Number", "AEM Lot Number",
        "AEM Date Code", "AEM Cage Code", "Customer Quality Clauses",
        "FAI Form 3", "Solderability Test Report", "DPA", "Visual Inspection Record",
        "Shipment Quantity", "Reel Labels", "Certificate of Conformance", "Route Sheet",
        "Part Number", "Lot Number", "Date", "Resistance", "Dimension", "Test Result"
    ]

    NUMERICAL_RANGES = {
        "Resistance": (95, 105),
        "Dimension": (0.9, 1.1)
    }

    anomalies = []
    critical_issues = []
    field_presence = defaultdict(int)
    all_fields = []

    def extract_fields(text):
        fields = {}
        for field in REQUIRED_FIELDS:
            pattern = rf"{field}[:\s]*([^\n]+)"
            match = re.search(pattern, text, re.IGNORECASE)
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
        text = extract_text_with_ocr(page)
        fields = extract_fields(text)
        all_fields.append(fields)

        for field in REQUIRED_FIELDS:
            if field not in fields:
                anomalies.append([page_num + 1, field, "Missing"])
            else:
                field_presence[field] += 1

        for field in NUMERICAL_RANGES:
            if field in fields and not validate_numerical(field, fields[field]):
                anomalies.append([page_num + 1, field, f"Out of range: {fields[field]}"])
                critical_issues.append([page_num + 1, field, fields[field]])

        # Update progress
        if progress_key:
            progress_store[progress_key] = int(((page_num + 1) / total_pages) * 100)

    for field in ["Part Number", "Lot Number", "Date"]:
        if not check_consistency(field):
            anomalies.append(["All Pages", field, "Inconsistent values"])
            critical_issues.append(["All Pages", field, "Inconsistent values"])

    with open(csv_path, "w", newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["Page", "Field", "Issue"])
        writer.writerows(anomalies)

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

    return csv_path, excel_path, dashboard_path, len(anomalies), len(critical_issues)

def validate_file(filepath, progress_key=None):
    # If PDF, run PDF validation, else fallback to dummy
    if filepath.lower().endswith('.pdf'):
        df = None
        csv_path, excel_path, dashboard_path, anomaly_count, critical_count = validate_pdf(filepath, EXPORTS_FOLDER, progress_key)
        # For download, return the CSV as DataFrame
        import logging
        logging.warning(f"[validate_file] CSV path: {csv_path} exists: {os.path.exists(csv_path)}")
        df = pd.read_csv(csv_path)
        if progress_key:
            progress_store[progress_key] = 100
        return df, os.path.basename(csv_path)
    else:
        data = {'filename': [os.path.basename(filepath)], 'status': ['validated']}
        df = pd.DataFrame(data)
        csv_filename = os.path.splitext(os.path.basename(filepath))[0] + '.csv'
        csv_path = os.path.join(EXPORTS_FOLDER, csv_filename)
        import logging
        df.to_csv(csv_path, index=False)
        logging.warning(f"[validate_file] Dummy CSV path: {csv_path} exists: {os.path.exists(csv_path)}")
        return df, csv_filename

def export_to_csv(df, csv_path):
    df.to_csv(csv_path, index=False)

@app.route('/', methods=['GET'])
def index():
    # Simple upload form
    return render_template_string('''
    <h2>Upload file for validation</h2>
    <form method="post" action="/api/validate" enctype="multipart/form-data">
        <input type="file" name="file">
        <input type="submit" value="Upload and Validate">
    </form>
    <div id="download-link"></div>
    <script>
    document.querySelector('form').onsubmit = async function(e) {
        e.preventDefault();
        const formData = new FormData(this);
        const res = await fetch('/api/validate', {method: 'POST', body: formData});
        const data = await res.json();
        if (data.csvFilename) {
            document.getElementById('download-link').innerHTML =
                `<a href="/download/${data.csvFilename}" download>Download CSV</a>`;
            window.location.href = `/download/${data.csvFilename}`;
        } else {
            document.getElementById('download-link').innerText = 'Validation failed.';
        }
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

        # Generate a unique progress key (could use session or uuid)
        progress_key = filename + str(int(time.time()))
        progress_store[progress_key] = 0

        def run_validation():
            validate_file(upload_path, progress_key)

        # Run validation in a background thread
        thread = threading.Thread(target=run_validation)
        thread.start()

        return jsonify({'progressKey': progress_key})
# Progress endpoint
@app.route('/api/progress/<progress_key>', methods=['GET'])
def get_progress(progress_key):
    percent = progress_store.get(progress_key, 0)
    return jsonify({'percent': percent})
    return jsonify({'error': 'Invalid file type'}), 400

@app.route('/download/<csv_filename>', methods=['GET'])
def download_csv(csv_filename):
    import logging
    try:
        full_path = os.path.join(app.config['EXPORTS_FOLDER'], csv_filename)
        logging.warning(f"[download_csv] Download requested: {full_path} exists: {os.path.exists(full_path)}")
        return send_from_directory(app.config['EXPORTS_FOLDER'], csv_filename, as_attachment=True)
    except FileNotFoundError:
        logging.error(f"[download_csv] File not found: {csv_filename}")
        return "File not found", 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=3000)

# Install Tesseract OCR
# RUN apt-get update && apt-get install -y tesseract-ocr
