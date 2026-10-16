# Use Python 3.11 Apify base image for optimal compatibility with audio libs
FROM apify/actor-python:3.11

# Switch to root to install system-level dependencies
USER root

# Prevent debconf interactive prompts during apt-get
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install system dependencies, ffmpeg, and build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    unzip \
    ffmpeg \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# Install Deno (official JavaScript runtime required by yt-dlp for EJS challenge solver)
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh \
    && chmod 755 /usr/local/bin/deno \
    && deno --version

# Switch back to non-root user
USER myuser

# Copy requirements first for Docker layer caching
COPY --chown=myuser:myuser requirements.txt ./

# Install Python dependencies
RUN echo "Python version:" \
 && python --version \
 && echo "Pip version:" \
 && pip --version \
 && echo "Installing dependencies:" \
 && pip install --no-cache-dir -r requirements.txt \
 && echo "All installed Python packages:" \
 && pip freeze

# Copy the remaining source code
COPY --chown=myuser:myuser . ./

# Validate Python source compiles correctly
RUN python3 -m compileall -q src/

# Launch the Actor
CMD ["python3", "-m", "src"]
