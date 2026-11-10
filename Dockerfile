FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mcc_monitor.py .
CMD ["python", "mcc_monitor.py", "--help"]
