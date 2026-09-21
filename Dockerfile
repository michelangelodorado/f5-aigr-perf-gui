FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY f5_guardrails_perf.py f5_find_max_rps.py f5_perf_prompts.csv server.py ./
COPY web/ ./web/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

VOLUME /app/data
EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
