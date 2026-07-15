# Third-party notices

OpenSoftware API depends on third-party Python packages, container images, model runtimes, and
hosted inference providers. They are not relicensed by this repository's MIT License.

- Python dependencies are declared in `pyproject.toml` and resolved with hashes in `uv.lock`.
  Installed wheels retain their upstream license metadata in their `.dist-info` directories.
- The optional vLLM containers are distributed by the vLLM project under its own license. The
  model weights they load may have separate terms; review and accept the applicable model license
  before downloading or redistributing weights.
- The local Compose profile uses Valkey, an open-source BSD-licensed Redis-compatible datastore.
- Docker, Kubernetes, Terraform, Google Cloud, OpenAI, Phala, Venice, Redis, Valkey, NVIDIA,
  Intel, and model-family names are the property of their respective owners. References describe
  interoperability and do not imply endorsement.

When redistributing a container or other binary artifact, preserve upstream license and notice
files and the generated software bill of materials.
