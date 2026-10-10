# Coverage service image: pygeoapi + CartoDEM provider + GDAL command-line tools, served by gunicorn
FROM python:3.12-slim-bookworm

# GDAL tools (gdal_translate, gdalbuildvrt) convert downloaded tiles and build the mosaic
RUN apt-get update \
    && apt-get install -y --no-install-recommends gdal-bin \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cartodem_service/ cartodem_service/
COPY scripts/ scripts/
COPY theme/ theme/
COPY pygeoapi-config.yml gunicorn.conf.py ./

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYGEOAPI_CONFIG=/app/pygeoapi-config.yml \
    PYGEOAPI_OPENAPI=/tmp/pygeoapi-openapi.yml

EXPOSE 5000

# Generate and correct the OpenAPI document (it depends on PYGEOAPI_SERVER_URL), then start gunicorn
CMD ["sh", "-c", "pygeoapi openapi generate \"$PYGEOAPI_CONFIG\" --output-file \"$PYGEOAPI_OPENAPI\" && python scripts/patch_openapi.py \"$PYGEOAPI_OPENAPI\" && exec gunicorn -c gunicorn.conf.py cartodem_service.app:APP"]
