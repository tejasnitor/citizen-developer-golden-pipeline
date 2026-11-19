# The fixture's image, so the self-test exercises the image job (build, Trivy, Syft).
FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
WORKDIR /app
COPY src/ src/
ENV PYTHONPATH=/app/src
RUN useradd --create-home app
USER app
CMD ["python", "-c", "import fixture_pkg; print(fixture_pkg.add(1, 2))"]
