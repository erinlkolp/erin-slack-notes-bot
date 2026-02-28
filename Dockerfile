FROM python:3.12

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN apt-get update && apt-get install -y default-mysql-client && rm -rf /var/lib/apt/lists/*

RUN useradd -m appuser
USER appuser

CMD ["python", "app.py"]
