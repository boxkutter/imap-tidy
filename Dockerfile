FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY translate_mail/ translate_mail/

# Starts as root only to chown /data/state, then switches to PUID:PGID
# (default 99:100, Unraid's nobody:users). See translate_mail/__main__.py.
VOLUME ["/data"]
CMD ["python", "-m", "translate_mail"]
