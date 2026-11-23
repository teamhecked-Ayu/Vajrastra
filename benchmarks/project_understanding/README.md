# Real repository retrieval benchmark

This benchmark uses three pinned, public Python repositories rather than generated directory layouts:

- Flask 3.1.3 (pallets/flask, tag 3.1.3)
- Requests 2.34.2 (psf/requests, tag v2.34.2)
- HTTPX at commit prefix 7cff5f7 (encode/httpx)

The expected source, test, and manifest paths are stored in real_benchmark.json.
The runner checks each checkout against the pinned ref before indexing it. It
reports per-repository required-path recall and precision.

## Run

Clone the repositories and check out the exact refs:

    git clone https://github.com/pallets/flask.git /work/flask
    git -C /work/flask checkout 3.1.3
    git clone https://github.com/psf/requests.git /work/requests
    git -C /work/requests checkout v2.34.2
    git clone https://github.com/encode/httpx.git /work/httpx
    git -C /work/httpx checkout 7cff5f7

    python benchmarks/project_understanding/run_real_benchmark.py --repo flask=/work/flask --repo requests=/work/requests --repo httpx=/work/httpx

The runner itself performs no network operations. It requires local Git checkouts.
The benchmark is a small, transparent retrieval benchmark, not proof of universal
semantic understanding. Add tasks and expected paths before changing ranking rules.
