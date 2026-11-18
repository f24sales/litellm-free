FROM docker.io/library/python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 XDG_STATE_HOME=/data
WORKDIR /app
COPY llm7_policy/ ./llm7_policy/
COPY requirements-import.txt ./
RUN python -m pip install -r requirements-import.txt
COPY import_litellm.py sql_import.py litellm_export.py config_files.py model_diff.py hook_client.py python_header.py config.conf_example env.example ./
RUN mkdir /data && chown 10001:10001 /data
USER 10001:10001
WORKDIR /data
ENTRYPOINT ["python", "/app/import_litellm.py"]
CMD ["api"]
