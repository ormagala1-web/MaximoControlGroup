FROM python:3.12-slim

WORKDIR /app

ENV BACKUP_AGENT_SECRET=OrmaBackup2026_Publicidad_7F9K3M2Q
ENV BACKUP_AGENT_PORT=8080

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY monitoreo21.py .
COPY agente_respaldo_remoto.py .
COPY bienvenidas.py .
COPY registro21.py .
COPY registro_grupos.json .
COPY bienvenidas_grupos.json .
COPY pertenencia_consulta.py .
COPY pertenencia_grupos.json .

RUN python -c "import bienvenidas, json; from pathlib import Path; g=json.loads(Path('bienvenidas_grupos.json').read_text(encoding='utf-8')); assert len(g)==21 and all(not x.get('registration_only',False) for x in g); print('Registro: modulo y mapa presentes')"

RUN python -c "import pertenencia_consulta, json; from pathlib import Path; g=json.loads(Path('pertenencia_grupos.json').read_text(encoding='utf-8')); assert len(g)==21 and all(not x.get('query_only',False) for x in g); print('Membresia obligatoria 21/21: OK')"

RUN python -c "import registro21, json; from pathlib import Path; g=json.loads(Path('registro_grupos.json').read_text(encoding='utf-8')); assert len(g)==21 and all(not x.get('registration_only',False) for x in g); print('Registro 21 grupos: OK')"

CMD ["python", "bot.py"]
