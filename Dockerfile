FROM python:3.11-slim

# Install system dependencies (including tesseract-ocr)
RUN apt-get update && apt-get install -y tesseract-ocr && rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . /app

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose the port Render will use
EXPOSE 10000

# Start the app with Gunicorn and a longer timeout for OCR, using the PORT env variable for compatibility
CMD ["sh", "-c", "gunicorn app.app:app --bind 0.0.0.0:${PORT:-10000} --timeout 120"]
