# -----------------------------------------------------------------------------
# Base Image and Environment Variables
# -----------------------------------------------------------------------------
FROM ubuntu:jammy
ENV DEBIAN_FRONTEND=noninteractive

# -----------------------------------------------------------------------------
# Install System Dependencies and Configure Timezone
# -----------------------------------------------------------------------------
RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    zlib1g-dev \
    libbz2-dev \
    libreadline-dev \
    libsqlite3-dev \
    wget \
    curl \
    llvm \
    libncurses5-dev \
    libncursesw5-dev \
    xz-utils \
    tk-dev \
    libffi-dev \
    liblzma-dev \
    python3-openssl \
    git \
    python3-opencv \
    python3-pip \
    python3-pyqt6* \
    pyqt6* \
    libxcb-cursor0 \
    zip

# -----------------------------------------------------------------------------
# Install Miniconda and Configure Shell Environment
# -----------------------------------------------------------------------------
RUN wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O /tmp/miniconda.sh && \
    bash /tmp/miniconda.sh -b -p /opt/conda && \
    rm /tmp/miniconda.sh

# Add conda to the PATH
ENV PATH="/opt/conda/bin:${PATH}"

# Set shell to bash with --login for proper Conda activation
SHELL ["/bin/bash", "--login", "-c"]

# -----------------------------------------------------------------------------
# Install Conda Packages 
# -----------------------------------------------------------------------------
RUN conda install -c conda-forge cupy python=3.11.11 pybind11 -y

# -----------------------------------------------------------------------------
# Set Working Directory and Prepare Application Dependencies
# -----------------------------------------------------------------------------
WORKDIR /app

# Upgrade pip and install Poetry
RUN /opt/conda/bin/python3.11 -m pip install --upgrade pip && \
    /opt/conda/bin/python3.11 -m pip install poetry --upgrade

# -----------------------------------------------------------------------------
# Disable Poetry Virtual Environment Creation
# -----------------------------------------------------------------------------
RUN /opt/conda/bin/python3.11 -m poetry config virtualenvs.create false

# -----------------------------------------------------------------------------
# Copy Application Code
# -----------------------------------------------------------------------------
COPY . /app

# -----------------------------------------------------------------------------
# Build and Install Application Using Poetry
# -----------------------------------------------------------------------------
# First, lock and install dependencies along with the local project
RUN /opt/conda/bin/python3.11 -m poetry lock && \
    /opt/conda/bin/python3.11 -m poetry install

# Build the project into a distributable wheel, then install that wheel
RUN /opt/conda/bin/python3.11 -m poetry build && \
    pip install dist/nebula_ai-*.whl

# -----------------------------------------------------------------------------
# Set Container Entrypoint
# -----------------------------------------------------------------------------
ENTRYPOINT ["nebula"]
