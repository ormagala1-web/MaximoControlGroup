FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY agente_respaldo_remoto.py .

CMD ["python", "bot.py"]
