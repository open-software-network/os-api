# Third-party notices

OpenSoftware API depends on third-party open-source packages listed in `pyproject.toml` and locked
in `uv.lock`. Those packages remain subject to their own licenses and copyright notices; the MIT
License for this repository does not replace them.

Direct runtime dependencies include FastAPI, Google Cloud client libraries, HTTPX, Jinja, Pydantic,
python-multipart, PyYAML, redis-py, and Uvicorn. Container, GitHub Actions, Terraform provider, and
Kubernetes dependencies are likewise governed by their upstream licenses.

Distributors should generate a license inventory from the exact lockfile and container image used
for their release and retain all notices required by those dependencies.
