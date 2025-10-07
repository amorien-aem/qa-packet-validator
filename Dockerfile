FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y tesseract-ocr && rm -rf /var/lib/apt/lists/*

# Set work directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the code
COPY . .

# Expose the port Render will use
ENV PORT=10000
EXPOSE 10000

# Start the app
CMD ["gunicorn", "app.app:app", "--bind", "0.0.0.0:10000"]
