FROM python:3.12-slim

WORKDIR /app

ENV BACKUP_AGENT_SECRET=OrmaBackup2026_Publicidad_7F9K3M2Q
ENV BACKUP_AGENT_PORT=8080

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY agente_respaldo_remoto.py .
COPY bienvenidas.py .
COPY bienvenidas_grupos.json .

RUN python -c "import bienvenidas, json; from pathlib import Path; assert len(json.loads(Path('bienvenidas_grupos.json').read_text(encoding='utf-8')))==12; print('Registro: modulo y mapa presentes')"

CMD ["python", "bot.py"]

