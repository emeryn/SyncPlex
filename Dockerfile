FROM alpine:latest

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apk add --no-cache \
    python3 \
    py3-pip \
    ca-certificates \
    && apk add --no-cache --virtual .build-deps \
    build-base \
    gcc \
    g++ \
    musl-dev \
    python3-dev \
    libffi-dev


RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY ./app /app

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]