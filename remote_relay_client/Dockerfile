FROM python:3.11-slim

WORKDIR /app
COPY app/ /app/
RUN pip install websockets requests

CMD ["python", "main.py"]
