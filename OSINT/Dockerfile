FROM python:3.12-slim

WORKDIR /app

# build-essential: some transitive deps (e.g. maigret's) may need to compile
# from sdist on platforms without a prebuilt wheel.
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# docker-compose.yml overrides this for the worker/migrate services.
CMD ["uvicorn", "osint_app.main:app", "--host", "0.0.0.0", "--port", "8000"]
