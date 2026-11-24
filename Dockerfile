FROM python:3.14-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libgomp1 git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY cli ./cli
COPY scripts ./scripts
# міграції потрібні всередині образу: схему створює alembic, а не create_all
COPY alembic ./alembic
COPY alembic.ini .
# Зібраний інтерфейс віддається тим самим застосунком.
# Збирати його треба заздалегідь: cd web && npm run build
COPY web/dist ./web/dist

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
