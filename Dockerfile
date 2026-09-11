FROM python:3.11-slim-bookworm

# Utilidades de medios: ffmpeg (transcodificación), MKVToolNix (mkvinfo/
# mkvpropedit para detección y etiquetado de idiomas en Matroska) y
# MediaInfo (lectura alternativa de metadatos).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        mkvtoolnix \
        mediainfo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Modelo de deteccion de idioma (fastText lid.176.ftz, ~1 MB). Va DENTRO de la
# imagen para que cualquier instalacion lo tenga sin descargar nada a mano:
# la app queda plug and play y la deteccion funciona incluso sin red.
RUN mkdir -p /opt/thresherr/models \
 && python -c "import shutil,urllib.request as u; r=u.urlopen('https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz', timeout=120); f=open('/opt/thresherr/models/lid.176.ftz','wb'); shutil.copyfileobj(r,f); f.close()" \
 && ls -l /opt/thresherr/models
COPY ./app /app

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
