FROM pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install TRL and dependencies
# We use the specific python from the base image's conda env
RUN /opt/conda/bin/python -m pip install --no-cache-dir \
    trl==0.14.0 \
    accelerate==1.13.0 \
    transformers==4.47.1 \
    peft==0.11.1 \
    datasets==2.21.0 \
    matplotlib \
    wandb

# Diagnostic check
RUN /opt/conda/bin/python -c "import trl; print(f'TRL Version: {trl.__version__}'); from trl import GRPOTrainer; print('Import Successful')"

# Install remaining requirements
COPY requirements.txt .
RUN /opt/conda/bin/python -m pip install --no-cache-dir -r requirements.txt

# Copy the entire codebase
COPY . .

# Install the TrustShield package
RUN /opt/conda/bin/python -m pip install -e .

# Set environment variables
ENV HF_HOME=/app/hf_cache
ENV TRANSFORMERS_CACHE=/app/hf_cache/transformers
ENV HF_DATASETS_CACHE=/app/hf_cache/datasets
ENV HUGGINGFACE_HUB_CACHE=/app/hf_cache/hub
ENV XDG_CACHE_HOME=/app/hf_cache
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Create cache and results directory and set permissions
RUN mkdir -p /app/hf_cache/transformers /app/hf_cache/datasets /app/hf_cache/hub /app/results && chmod -R 777 /app/hf_cache /app/results

# Use the full path for the command
CMD ["/opt/conda/bin/python", "training/train_grpo1.py"]
