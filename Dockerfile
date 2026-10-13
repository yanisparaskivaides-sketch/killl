FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip && python -m pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
COPY main.py ./main.py
RUN mkdir -p /app/bot/data /app/bot/backups
CMD ["python", "-m", "bot.main"]
