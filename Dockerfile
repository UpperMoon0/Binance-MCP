FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 10001 binance
COPY pyproject.toml README.md LICENSE ./
COPY binance_mcp ./binance_mcp
COPY main.py ./main.py
RUN pip install --no-cache-dir .
RUN mkdir -p /app/data && chown -R binance:binance /app
USER binance
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
