FROM python:3.11-slim

# Install system dependencies required for OCR, PDF/image handling and matplotlib
RUN apt-get update \
	&& DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
	   tesseract-ocr \
	   poppler-utils \
	   libgl1 \
	   libglib2.0-0 \
	   libsm6 \
	   libxext6 \
	   libxrender1 \
	&& rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . /app

# Install Python dependencies (use --no-cache-dir to reduce image size)
RUN pip install --no-cache-dir -r requirements.txt

# Expose the port Render will use (default overridden by PORT env var)
EXPOSE 10000

# Start the app with Gunicorn and a longer timeout for OCR-heavy jobs
CMD ["sh", "-c", "gunicorn app.app:app --bind 0.0.0.0:${PORT:-10000} --timeout 60 --workers 1"]
