FROM continuumio/miniconda3:latest
ENV DEBIAN_FRONTEND=noninteractive
# Install only essential system packages for building native extensions
RUN apt-get update && apt-get install -y \
        build-essential \
        cmake \
        git \
        tzdata \
    && ln -fs /usr/share/zoneinfo/America/New_York /etc/localtime \
    && dpkg-reconfigure --frontend noninteractive tzdata \
    && apt-get clean && rm -rf /var/lib/apt/lists/


# Set working directory
WORKDIR /app

# Use conda to install packages (e.g., cupy, python, pybind11)
RUN conda install -c conda-forge cupy python=3.11.11 pybind11 -y

# Upgrade pip and install Poetry
RUN pip install --upgrade pip && pip install poetry --upgrade && \
    poetry config virtualenvs.create false

# Copy your application code
COPY . /app

# Install application dependencies via Poetry
RUN poetry lock && poetry install

# Install pip-only package
RUN pip install nebula-ai --upgrade


# Final Entrypoint
ENTRYPOINT ["nebula"]
