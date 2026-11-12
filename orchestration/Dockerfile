# The image Kestra starts for every Mend step (ingest, build, repair, apply, ...).
# Build context is the repository root:  docker compose -f orchestration/docker-compose.yml build mend-image
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GIT_PYTHON_REFRESH=quiet \
    MEND_HOME=/mend_home

WORKDIR /app

# An editable install keeps /app as the project root, which is where mend finds its dbt project (dbt/mend_dbt).
# constraints.txt pins the versions the evaluation ran with.
COPY pyproject.toml constraints.txt ./
COPY src ./src
COPY dbt ./dbt
RUN pip install -c constraints.txt -e ".[agent]"

COPY orchestration/mend_ops.py ./orchestration/mend_ops.py
RUN printf '#!/bin/sh\nexec python /app/orchestration/mend_ops.py "$@"\n' > /usr/local/bin/mend-ops \
 && chmod +x /usr/local/bin/mend-ops \
 && mkdir -p /mend_home

# The state (warehouse, dlt, the live dbt project, repair proposals) lives in this volume, not in the image.
VOLUME /mend_home
CMD ["mend-ops", "status"]
