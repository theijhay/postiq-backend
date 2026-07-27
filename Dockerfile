# Use official slim Python image
FROM python:3.12.4-slim-bullseye

# Prevents Python from writing .pyc files to disc
ENV PYTHONDONTWRITEBYTECODE 1
# Ensures output is logged instantly (not buffered)
ENV PYTHONUNBUFFERED 1
ENV PYTHONPATH=/app

# Create working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Expose port (used in both dev and prod)
EXPOSE 8000

# Entrypoint command (development). APP_PORT matches api/utils/settings.py.
CMD ["sh", "-c", "alembic upgrade head && uvicorn main:app --host 0.0.0.0 --port ${APP_PORT:-8000}"]