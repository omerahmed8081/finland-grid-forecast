FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY dashboard ./dashboard
RUN pip install --no-cache-dir .
ENV PYTHONPATH=/app/src
EXPOSE 8000 8501
