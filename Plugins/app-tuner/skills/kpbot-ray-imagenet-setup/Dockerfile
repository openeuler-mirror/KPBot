FROM python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
WORKDIR /skill
ARG PIP_NO_INDEX=
ARG PIP_FIND_LINKS=
COPY requirements.txt requirements-lock.txt requirements-cpu-lock.txt ./
RUN export PIP_NO_INDEX="${PIP_NO_INDEX}" PIP_FIND_LINKS="${PIP_FIND_LINKS}" \
    && python -m pip install --no-cache-dir --require-hashes --only-binary=:all: --no-deps \
        --report /torch-install.json --index-url https://download-r2.pytorch.org/whl/cpu \
        -r requirements-cpu-lock.txt \
    && python -m pip install --no-cache-dir --require-hashes --only-binary=:all: \
        --report /python-install.json --index-url https://pypi.org/simple \
        -r requirements-lock.txt \
    && python -m pip check \
    && python -m pip freeze > /installed-packages.txt
COPY SKILL.md ./
COPY scripts/check_environment.py scripts/make_fixture.py scripts/run_pipeline.py ./scripts/
COPY cases/image-smoke/case.yaml cases/image-smoke/check.py cases/image-smoke/run.sh cases/image-smoke/test_exit_codes.py ./cases/image-smoke/
CMD ["python", "scripts/check_environment.py"]
