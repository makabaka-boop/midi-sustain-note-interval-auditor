FROM python:3.12-slim

WORKDIR /app
COPY sustain ./sustain

# Reads one JSON document from stdin, writes the audit result to stdout.
ENTRYPOINT ["python", "-m", "sustain"]
