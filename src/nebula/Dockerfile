FROM kalilinux/kali-bleeding-edge:amd64

LABEL org.opencontainers.image.authors="david@berylliumsec.com"

RUN apt update && \
    apt upgrade -y && \
    apt-get install -y --no-install-recommends \
        kali-linux-headless \
        libreadline-dev && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* 

ENV PYTHONUNBUFFERED=1 \
    IN_DOCKER=yes

WORKDIR /app

COPY nebula/requirements.txt /app/

RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY nebula/nebula.py nebula/nmap_flags nebula/nuclei_flags nebula/zap_flags nebula/crackmap_flags /app/
COPY nebula/indexdir /app/indexdir

CMD ["python", "nebula.py", "--model_dir", "/app/unified_models_no_zap"]
