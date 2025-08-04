FROM python:3.12

WORKDIR /app

COPY . .

RUN apt-get update

RUN apt-get install default-mysql-client -y

RUN pip install -r requirements.txt

CMD ["python", "app.py"]