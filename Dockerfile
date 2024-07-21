# Use the official Python image as a base image
FROM python:3.10.6

# Set the working directory in the container
WORKDIR /app

# Copy the requirements.txt file into the container
COPY requirements.txt .

# Install any dependencies
RUN pip install --upgrade setuptools

RUN pip install -r requirements.txt

# Copy the current directory contents into the container at /app
COPY . /app

ENTRYPOINT ["python", "main.py"]
