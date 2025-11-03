# Use the official Python image as a base image
FROM python:3.10-slim

# Set UTC timezone
ENV TZ=UTC
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Set the working directory in the container
WORKDIR /app

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

# Upgrade pip and setuptools to avoid potential issues with older versions
RUN pip install --upgrade pip setuptools

# Install any dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Set the entry point for the container
ENTRYPOINT ["python", "main.py"]
