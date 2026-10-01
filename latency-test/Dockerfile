FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir flask==3.1.2 gunicorn==23.0.0

COPY main.py .

ENV PORT=8080
EXPOSE 8080

CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "2", "--timeout", "120", "main:app"]
