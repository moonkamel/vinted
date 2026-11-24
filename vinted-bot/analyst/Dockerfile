FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY analyst.py reference.md rules.json ./
CMD ["python", "-u", "analyst.py"]
