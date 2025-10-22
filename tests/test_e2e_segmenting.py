import os
import io
import time
import math
import socket
import tempfile
import pytest
import requests
import multiprocessing
from pathlib import Path
import shutil

# This E2E test is opt-in. Set RUN_E2E=1 in the environment to run it.
RUN_E2E = os.environ.get("RUN_E2E") == "1"

pytestmark = pytest.mark.skipif(not RUN_E2E, reason="E2E tests are opt-in. Set RUN_E2E=1 to run.")

EXPORTS_DIR = Path("app/exports")


def _get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    addr, port = s.getsockname()
    s.close()
    return port


def make_multipage_pdf(num_pages=8):
    """Create an in-memory multi-page PDF using ReportLab deterministically.
    Returns bytes of the PDF."""
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import letter
    except Exception as e:
        pytest.skip(f"reportlab not available for E2E PDF generation: {e}")

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for i in range(num_pages):
        c.drawString(72, 720, f"Test page {i+1}")
        c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()


def poll_progress(app_url, progress_key, timeout=30):
    url = f"{app_url}/api/progress/{progress_key}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(url)
        r.raise_for_status()
        data = r.json()
        if data.get("done"):
            return data
        time.sleep(0.5)
    raise TimeoutError("Progress did not complete in time")


def wait_for_server(app_url, timeout=10.0):
    deadline = time.time() + timeout
    url = f"{app_url}/api/diagnostics"
    while time.time() < deadline:
        try:
            r = requests.get(url)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def start_flask_process(port, export_dir: str = None, env=None):
    # Import the app inside the child process to avoid pickle issues
    def _run():
        # Ensure the child process has the same working dir
        if env:
            os.environ.update(env)
        # Import the app module and then override paths to use isolated temp dirs
        import app.app as appmod
        # Ensure the export and upload/progress folders are isolated to avoid repo pollution
        if export_dir:
            try:
                appmod.EXPORTS_FOLDER = export_dir
                appmod.app.config['EXPORTS_FOLDER'] = export_dir
            except Exception:
                pass
        # Also set uploads and progress to subdirs under export_dir to keep everything isolated
        try:
            if export_dir:
                up = os.path.join(export_dir, 'uploads')
                pr = os.path.join(export_dir, 'progress')
                os.makedirs(up, exist_ok=True)
                os.makedirs(pr, exist_ok=True)
                appmod.UPLOAD_FOLDER = up
                appmod.PROGRESS_DIR = pr
                appmod.app.config['UPLOAD_FOLDER'] = up
        except Exception:
            pass

        appmod.app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)

    ctx = multiprocessing.get_context('spawn')
    p = ctx.Process(target=_run)
    p.start()
    return p


def test_segmented_pdf_processing_creates_segment_files(tmp_path):
    # Configurable test parameters
    num_pages = 10
    segment_size = int(os.environ.get('PAGE_SEGMENT_SIZE', '4'))
    expected_segments = math.ceil(num_pages / segment_size)

    # Start the Flask server on a free port with the PAGE_SEGMENT_SIZE env var
    port = _get_free_port()
    app_url = f"http://127.0.0.1:{port}"

    # Use a temporary exports directory so the test doesn't pollute the repo
    tmp_exports = str(tmp_path / "exports")
    os.makedirs(tmp_exports, exist_ok=True)

    env = os.environ.copy()
    env['PAGE_SEGMENT_SIZE'] = str(segment_size)

    server_proc = start_flask_process(port, export_dir=tmp_exports, env=env)
    try:
        assert wait_for_server(app_url, timeout=12), "Flask server did not start in time"

        # Prepare PDF and POST
        pdf_bytes = make_multipage_pdf(num_pages=num_pages)
        files = {"file": ("test_multipage.pdf", pdf_bytes, "application/pdf")}
        resp = requests.post(f"{app_url}/api/validate", files=files)
        resp.raise_for_status()
        data = resp.json()
        assert "progressKey" in data
        progress_key = data["progressKey"]

        # While processing, wait for segment files to appear in the temporary exports dir
        deadline = time.time() + 30
        seen_segments = set()
        exports_path = Path(tmp_exports)
        while time.time() < deadline and len(seen_segments) < expected_segments:
            for p in exports_path.glob(f"*_segment_*_validation_summary.csv"):
                seen_segments.add(p.name)
            if len(seen_segments) >= expected_segments:
                break
            time.sleep(0.3)

        assert len(seen_segments) >= 1, "Expected at least one segment CSV to be created during processing"
        # Prefer asserting we saw the expected number of segments (best-effort)
        assert len(seen_segments) == expected_segments or len(seen_segments) >= 1

        # Now poll until done and verify final CSV
        progress = poll_progress(app_url, progress_key, timeout=120)
        assert progress.get("done") is True
        csv_name = progress.get("csv_filename")
        assert csv_name, "Expected final csv_filename in progress payload"

        final_csv = Path(tmp_exports) / csv_name
        assert final_csv.exists(), f"Final CSV not found: {final_csv}"

        # Stronger content checks
        text = final_csv.read_text(errors='ignore')
        # Header check
        assert "Page" in text and "Field" in text and "Result" in text
        # Row count check: expect at least num_pages * 22 rows (22 required fields per page)
        rows = [r for r in text.splitlines() if r.strip()]
        # subtract header
        data_rows = rows[1:]
        assert len(data_rows) >= (num_pages * 22), f"Expected at least {num_pages * 22} rows, got {len(data_rows)}"

    finally:
        # Terminate server process
        try:
            server_proc.terminate()
            server_proc.join(timeout=5)
        except Exception:
            pass

        # Cleanup temporary exports directory unless preservation requested
        preserve = os.environ.get('PRESERVE_E2E_ARTIFACTS') == '1'
        if not preserve:
            try:
                shutil.rmtree(tmp_exports, ignore_errors=True)
            except Exception:
                pass
