FROM ghcr.io/astral-sh/uv:0.9.9 AS uv

FROM debian:bookworm-slim AS company-office
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv libreoffice-calc fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock README.md CONTRIBUTING.md LICENSE /opt/dependencies/
RUN UV_PROJECT_ENVIRONMENT=/opt/python uv sync --project /opt/dependencies --frozen --only-group office-worker --python /usr/bin/python3 \
    && rm -rf /root/.cache/uv /opt/dependencies
COPY company_agent/services/office/worker.py /app/office_worker.py
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 LANG=C.UTF-8
USER 1000:1000
ENTRYPOINT ["/opt/python/bin/python", "/app/office_worker.py"]

FROM company-office AS eval-preview
USER 0:0
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-writer libreoffice-impress \
    && rm -rf /var/lib/apt/lists/*
USER 1000:1000

FROM debian:bookworm-slim AS eip-office
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer fonts-noto-cjk fonts-arphic-ukai \
    && rm -rf /var/lib/apt/lists/*
ENV HOME=/work LANG=C.UTF-8
WORKDIR /work
ENTRYPOINT ["libreoffice", "-env:UserInstallation=file:///work/profile", "--headless", "--nologo", "--nodefault", "--nofirststartwizard", "--norestore"]

FROM mcr.microsoft.com/dotnet/sdk:8.0 AS cad-build
WORKDIR /src
COPY company_agent/services/cad/worker/CadReader.csproj ./
RUN dotnet restore
COPY company_agent/services/cad/worker/Program.cs ./
RUN dotnet publish -c Release --no-restore -o /app

FROM mcr.microsoft.com/dotnet/runtime:8.0-bookworm-slim AS company-cad
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock README.md CONTRIBUTING.md LICENSE /opt/dependencies/
RUN UV_PROJECT_ENVIRONMENT=/opt/cad-python uv sync --project /opt/dependencies --frozen --only-group cad-worker --python /usr/bin/python3 \
    && rm -rf /root/.cache/uv /opt/dependencies
COPY --from=cad-build /app /app
COPY company_agent/services/cad/worker/dxf_worker.py /app/dxf_worker.py
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
USER 1000:1000

# Keep the API as the default target; sandbox images use an explicit --target.
FROM python:3.11-slim AS management-api
COPY --from=uv /uv /usr/local/bin/uv
RUN useradd --create-home --uid 1000 appuser
WORKDIR /app
COPY pyproject.toml uv.lock README.md CONTRIBUTING.md LICENSE /app/
COPY --chown=appuser:appuser examples/__init__.py /app/examples/
COPY --chown=appuser:appuser examples/identity /app/examples/identity
COPY --chown=appuser:appuser examples/bff /app/examples/bff
COPY --chown=appuser:appuser examples/db_management /app/examples/db_management
COPY --chown=appuser:appuser examples/deployment/__init__.py examples/deployment/cli.py /app/examples/deployment/
COPY --chown=appuser:appuser examples/deployment/registry /app/examples/deployment/registry
COPY --chown=appuser:appuser examples/shared /app/examples/shared
RUN uv sync --frozen --no-dev --extra identity-services \
    && rm -rf /root/.cache/uv
ENV PATH=/app/.venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER appuser
EXPOSE 8005
CMD ["uvicorn", "examples.db_management.app:app", "--host", "0.0.0.0", "--port", "8005"]
