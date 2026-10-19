FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --system --uid 10001 --no-create-home exporter
USER 10001
EXPOSE 9877
ENTRYPOINT ["metrics-exporter"]
CMD ["serve", "--bind", "0.0.0.0", "--port", "9877"]
