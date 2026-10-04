FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
COPY run.py setup_env.py config.yaml ./
RUN useradd -r -u 1000 copybot && mkdir -p /app/data && chown -R copybot /app/data
USER copybot
VOLUME ["/app/data"]
EXPOSE 8000
CMD ["python", "run.py"]
