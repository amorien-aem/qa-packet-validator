#!/usr/bin/env python3
"""
Simple smoke test for the local docker-compose stack.
- Uploads `app/uploads/qachecklistfusetest.pdf` to the local web service
- Polls `/api/progress/<progressKey>` until done or timeout
- Downloads CSV (or error CSV) to `compose/outputs/`

Usage:
    python compose/smoke_test.py --url http://localhost:3000 --timeout 300

Requires: requests (pip install requests)
"""

import argparse
import json
import os
import time
import sys

try:
    import requests
except ImportError:
    print('This script requires the requests library: pip install requests')
    sys.exit(2)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, '..'))
UPLOAD_PATH = os.path.join(PROJECT_ROOT, 'app', 'uploads', 'qachecklistfusetest.pdf')
OUTPUT_DIR = os.path.join(SCRIPT_DIR, 'outputs')
os.makedirs(OUTPUT_DIR, exist_ok=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--url', default='http://localhost:3000', help='Base URL for the web service')
    p.add_argument('--timeout', type=int, default=300, help='Timeout in seconds')
    p.add_argument('--poll-interval', type=float, default=1.0, help='Seconds between progress polls')
    args = p.parse_args()

    if not os.path.exists(UPLOAD_PATH):
        print('Upload sample PDF not found at', UPLOAD_PATH)
        sys.exit(1)

    files = {'file': open(UPLOAD_PATH, 'rb')}
    try:
        print('Uploading sample PDF to', args.url)
        resp = requests.post(f'{args.url}/api/validate', files=files, timeout=30)
    finally:
        files['file'].close()

    if resp.status_code != 200:
        print('Upload failed:', resp.status_code, resp.text)
        sys.exit(1)

    data = resp.json()
    progress_key = data.get('progressKey')
    if not progress_key:
        print('No progressKey returned:', data)
        sys.exit(1)

    print('Progress key:', progress_key)

    start = time.time()
    last = None
    while True:
        if time.time() - start > args.timeout:
            print('Timed out waiting for job completion')
            sys.exit(1)
        try:
            pr = requests.get(f'{args.url}/api/progress/{progress_key}', timeout=10).json()
        except Exception as e:
            print('Error polling progress:', e)
            time.sleep(args.poll_interval)
            continue
        percent = pr.get('percent', 0)
        done = pr.get('done', False)
        csv_filename = pr.get('csv_filename') or pr.get('download_url')
        if pr != last:
            print('Progress:', percent, 'done=', done, 'csv_filename=', csv_filename)
            last = pr
        if done:
            # If progress returned a download_url, use it; otherwise build download path
            download_url = pr.get('download_url')
            if download_url:
                print('Downloading via presigned URL...')
                dl = requests.get(download_url, timeout=30)
                if dl.status_code == 200:
                    out_path = os.path.join(OUTPUT_DIR, csv_filename or f'{progress_key}.csv')
                    with open(out_path, 'wb') as f:
                        f.write(dl.content)
                    print('Saved:', out_path)
                    sys.exit(0)
                else:
                    print('Failed to download from presigned URL:', dl.status_code)
                    sys.exit(1)
            else:
                if not csv_filename:
                    print('Job done but no csv_filename provided in progress.')
                    sys.exit(1)
                dl = requests.get(f'{args.url}/download/{csv_filename}', timeout=30)
                if dl.status_code in (200, 201):
                    out_path = os.path.join(OUTPUT_DIR, csv_filename)
                    with open(out_path, 'wb') as f:
                        f.write(dl.content)
                    print('Saved:', out_path)
                    sys.exit(0)
                else:
                    print('Failed to download CSV:', dl.status_code, dl.text)
                    sys.exit(1)
        time.sleep(args.poll_interval)


if __name__ == '__main__':
    main()
