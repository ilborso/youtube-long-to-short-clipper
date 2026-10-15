# Use Python 3.11 Apify base image for optimal compatibility with audio libs
FROM apify/actor-python:3.11

# Switch to root to install system-level dependencies
USER root

# Install ffmpeg and build tools required for audio processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

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
