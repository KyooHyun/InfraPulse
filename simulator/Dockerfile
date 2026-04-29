FROM python:3.11-slim

WORKDIR /simulator
ENV PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY simulator.py ./

CMD ["python", "simulator.py"]
