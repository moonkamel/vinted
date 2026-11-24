FROM python:3.12-slim
# Tesseract : lecture des étiquettes sur les photos (OCR), en anglais et en français
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-fra \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY analyst.py ocr.py reference.md *.json ./
CMD ["python", "-u", "analyst.py"]
