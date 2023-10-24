FROM kalilinux/kali-bleeding-edge:amd64
ARG APP_VERSION
ENV DOCKER_APP_VERSION=$APP_VERSION
ENV IN_DOCKER=yes
LABEL org.opencontainers.image.authors="david@berylliumsec.com"

RUN apt update && \
    apt upgrade -y && \
    apt-get install -y --no-install-recommends \
        kali-linux-headless \
        libreadline-dev && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* 

WORKDIR /
RUN git clone https://github.com/scipag/vulscan scipag_vulscan && ln -s `pwd`/scipag_vulscan /usr/share/nmap/scripts/vulscan
WORKDIR /app

COPY nebula/requirements.txt /app/

RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY nebula/nebula.py nebula/nmap_flags nebula/nuclei_flags nebula/zap_flags nebula/crackmap_flags nebula/suggestions /app/
COPY nebula/indexdir /app/indexdir
COPY nebula/indexdir_auto /app/indexdir_auto

# Define the entry point
ENTRYPOINT ["python", "nebula.py"]
CMD ["--model_dir", "/app/unified_models_no_zap"]