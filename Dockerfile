FROM python:3.12-slim

WORKDIR /app

ENV BACKUP_AGENT_SECRET=OrmaBackup2026_Publicidad_7F9K3M2Q
ENV BACKUP_AGENT_PORT=8080

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY agente_respaldo_remoto.py .

CMD ["python", "bot.py"]

