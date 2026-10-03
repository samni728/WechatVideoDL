FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py browserless_function.js ./
RUN mkdir -p /data/downloads

EXPOSE 8080

CMD ["gunicorn","-w","1","--threads","4","--timeout","420","-b","0.0.0.0:8080","app:app"]
