# One image, two entrypoints: the API (default) and the worker
# (`python -m app.worker`). Deploying the same image twice on Railway keeps
# the two services on identical code without a second build.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg/ffprobe come from the distribution and land on PATH, which is why
# app/video/ffmpeg.py no longer needs its three-tier binary search.
#
# fonts-dejavu-core and fonts-noto-core are NOT optional: burned-in subtitles
# are rendered by libass against a font installed in this image, and the
# default container font set has no Cyrillic coverage — Mongolian text would
# render as empty boxes in every exported video.
#
# fonts-inter is the subtitle default. SF Pro is the web UI's face and Apple
# licenses it for Apple platforms only, so it cannot be shipped in a Linux
# render container; Inter is metrically close, openly licensed, and carries
# the Cyrillic Mongolian needs.
#
# fontconfig is what libass resolves a font NAME through, and it also brings
# `fc-list` — which is the only way to answer "does this image actually have
# a Cyrillic face" without rendering a video and looking at it. CI asks
# exactly that question, and app/subtitle/fonts.py asks it at run time so the
# settings page can only ever offer a font that is really here: libass does
# not fail on a missing family, it silently substitutes another.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
        fonts-noto-core \
        fonts-inter \
        fontconfig \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The clean dub — Demucs removing the source speech under the Mongolian
# voice — is built into the WORKER only. Set INSTALL_DUB=1 as a variable on
# the worker service: Railway passes a service variable into a Dockerfile
# build only when an ARG names it, and the API service, which never sets it,
# keeps the slim image (no torch: see requirements.txt for why).
ARG INSTALL_DUB=0
ARG DEMUCS_MODEL=htdemucs

COPY requirements.txt requirements-dub.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_DUB" = "1" ]; then pip install --no-cache-dir -r requirements-dub.txt; fi

# The weights are baked in, not fetched on the first dub: a download in the
# middle of an export is one more thing that can fail it, and a restart
# would fetch them again. Before the app is copied, so a code change does
# not throw this layer away. The path is the one the app reads
# (app.config.Settings.model_cache_dir).
ENV MODEL_CACHE_DIR=/opt/models
RUN if [ "$INSTALL_DUB" = "1" ]; then \
        python -c "import torch; torch.hub.set_dir('$MODEL_CACHE_DIR'); \
from demucs.pretrained import get_model; get_model('$DEMUCS_MODEL')"; \
    fi

COPY alembic.ini ./
COPY app ./app

# Scratch space. Nothing here survives a restart by design — every artifact
# worth keeping is uploaded to R2.
ENV WORK_DIR=/tmp/monireels
RUN mkdir -p /tmp/monireels

# Railway injects PORT; this is only the local default.
ENV PORT=8000
EXPOSE 8000

CMD ["python", "-m", "app.main"]
