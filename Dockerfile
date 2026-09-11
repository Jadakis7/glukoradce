FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    GLUKORADCE_DB=/data/glukoradce.sqlite \
    GLUKORADCE_AUTORELOAD=0 \
    GLUKORADCE_HTTPS=1 \
    PORT=8765
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8765
CMD ["python", "app.py"]
