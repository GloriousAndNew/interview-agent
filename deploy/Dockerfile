FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
WORKDIR /app
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY requirements.lock.txt requirements-server.lock.txt ./
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements-server.lock.txt
COPY pyproject.toml app.py worker.py ./
COPY interview_agent ./interview_agent
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir /data && chown app:app /data
USER 10001:10001
ENV SERVER_MODE=1 SERVER_DATA_DIR=/data
CMD ["python", "-m", "interview_agent.server_api"]
