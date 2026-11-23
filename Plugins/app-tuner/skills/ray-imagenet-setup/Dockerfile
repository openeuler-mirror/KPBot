FROM python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
WORKDIR /skill
ARG PYPI_INDEX_URL=https://pypi.org/simple
COPY requirements.txt requirements-lock.txt ./
RUN python -m pip install --no-cache-dir --timeout 30 --retries 2 --no-deps --report /torch-install.json --index-url https://download.pytorch.org/whl/cpu torch==2.6.0 torchvision==0.21.0 \
    && python -m pip install --no-cache-dir --timeout 30 --retries 2 --report /python-install.json --index-url ${PYPI_INDEX_URL} -r requirements-lock.txt torch==2.6.0 torchvision==0.21.0 \
    && python -m pip check \
    && python -m pip freeze > /installed-packages.txt
COPY . .
CMD ["python", "scripts/check_environment.py"]
