# Deployment

## Container

```bash
docker build -t forensic-workbench:0.3.0 .
docker compose up            # http://127.0.0.1:8000
docker compose --profile agents up   # also the authenticate MCP server on the internal network
```

`docker-compose.yml` puts the service on an internal network with no route out. Case data
lives in `./data`; `./import` is the only folder agents may ingest from.

## Air-gapped installation

On a connected machine with the same OS, CPU architecture and Python version as the target:

```bash
deploy/airgap/build-bundle.sh            # add --docker to include the container image
```

This writes `dist/forensic-workbench-<version>-airgap/` with every Python wheel, the backend
with its model weights, the built UI, the docs and `SHA256SUMS`. Carry it across, install
FFmpeg from the target OS's offline packages, then:

```bash
./install.sh /opt/forensic-workbench
/opt/forensic-workbench/run.sh
```

`install.sh` checks every file against `SHA256SUMS`, installs with `pip --no-index`, and runs
the acceptance self-test.

## Acceptance self-test

```bash
python -m forensic.selftest
```

Checks FFmpeg, re-hashes every model, checks ONNX Runtime gives bit-identical output on this
CPU, loads the face detector and the C2PA reader, then runs a small case end to end
(ingest, render, reproduce, triage, measure, review, PDF report) in a temporary folder. Run
it after every installation or upgrade and keep its output with the lab's validation records.

## No network at run time

- Models are bundled or installed locally; weights are verified before every load.
- C2PA manifests are validated offline (no OCSP or remote-manifest fetching). To recognise
  signers your lab trusts, set `FORENSIC_C2PA_TRUST_ANCHORS` to a PEM file of root
  certificates; without it signatures are checked for integrity and reported as from an
  untrusted signer.
- The built-in agents (triage, planner, reviewer) are deterministic programs, not LLM calls.
- An LLM agent connects through the MCP servers (`python -m forensic.mcp_server <server>
  --http PORT`). Any MCP client works, so a self-hosted model on the same network can drive
  the workbench; nothing in the workbench calls an external model API.
