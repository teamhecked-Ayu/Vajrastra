# Real repository retrieval benchmark

This benchmark uses three pinned, public Python repositories rather than generated directory layouts:

- Flask 3.1.3 (pallets/flask, commit 22d924701a6ae2e4cd01e9a15bbaf3946094af65)
- Requests 2.34.2 (psf/requests, commit 6e83187b8feb273ed4c6cdab5efd8d54901dfab3)
- HTTPX (encode/httpx, commit 7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016)

The expected source, test, and manifest paths are stored in real_benchmark.json.
The runner checks each checkout against the pinned ref before indexing it. It
reports per-repository required-path recall and precision.

## Run

Clone the repositories and check out the exact refs:

    git clone https://github.com/pallets/flask.git /work/flask
    git -C /work/flask checkout 22d924701a6ae2e4cd01e9a15bbaf3946094af65
    git clone https://github.com/psf/requests.git /work/requests
    git -C /work/requests checkout 6e83187b8feb273ed4c6cdab5efd8d54901dfab3
    git clone https://github.com/encode/httpx.git /work/httpx
    git -C /work/httpx checkout 7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016

    python benchmarks/project_understanding/run_real_benchmark.py --repo flask=/work/flask --repo requests=/work/requests --repo httpx=/work/httpx

The runner itself performs no network operations. It requires local Git checkouts.
The benchmark is a small, transparent retrieval benchmark, not proof of universal
semantic understanding. Add tasks and expected paths before changing ranking rules.
