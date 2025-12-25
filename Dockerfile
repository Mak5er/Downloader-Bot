# Use the official Python image as a base image
FROM python:3.10-slim

# Set UTC timezone
ENV TZ=UTC
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Set the working directory in the container
WORKDIR /app

RUN sed -i 's|deb.debian.org|ftp.de.debian.org|g' \
    /etc/apt/sources.list.d/debian.sources

# Install ffmpeg and other required system packages
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        gcc \
        libffi-dev \
        libssl-dev \
        build-essential \
        && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy the requirements.txt file into the container
COPY requirements.txt .

# Set pip mirror before install
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple/ && \
    pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn

# Upgrade pip and setuptools to avoid potential issues with older versions
RUN pip install --upgrade pip setuptools

# Install any dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Set the entry point for the container
ENTRYPOINT ["python", "main.py"]