FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY agent ./agent
RUN pip install --upgrade pip && pip install .

COPY playbook.md ./

RUN useradd --create-home --uid 10001 agent && chown -R agent /app
USER agent

CMD ["python", "-m", "agent.main"]
