import os
from app import sanitize_csv_filename, set_progress, get_progress_data, PROGRESS_DIR
import uuid

def test_sanitize_csv_filename():
    assert sanitize_csv_filename('normal.csv') == 'normal.csv'
    assert sanitize_csv_filename('weird name!.CSV') == 'weird_name.csv'
    assert sanitize_csv_filename('noext') == 'noext.csv'


def test_progress_error_field(tmp_path):
    # Use a temporary progress key and write to the PROGRESS_DIR
    key = str(uuid.uuid4())
    # Ensure no existing file
    p = os.path.join(PROGRESS_DIR, f"{key}.json")
    if os.path.exists(p):
        os.remove(p)
    # Set progress with an error using new signature
    set_progress(key, percent=100, csv_filename=None, done=True, error={'message': 'test error'})
    data = get_progress_data(key)
    assert data['percent'] == 100
    assert data['done'] is True
    assert isinstance(data['error'], dict)
    assert data['error'].get('message') == 'test error'
