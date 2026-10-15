# AI models

The plan's rule is *classical first, AI second*. Every learned model Lucida runs is
listed in a pinned manifest, verified before each load, run offline on the CPU, and named
with its weights hash in the report next to every step or analysis that used it.

## Bundled models

| Id | Task | Licence | Used by |
|---|---|---|---|
| `sr.fsrcnn_x2/x3/x4` | Single-image super resolution (FSRCNN, Dong et al. 2016) | Apache-2.0 | `ai.super_resolution` (generative) |
| `sr.espcn_x2/x3/x4` | Single-image super resolution (ESPCN, Shi et al. 2016) | Apache-2.0 | `ai.super_resolution` (generative) |
| `face.yunet` | Face detection (YuNet, Wu et al. 2023) | MIT | `face.consistency`, `redact.faces` |

Sources, training data and limits are in `backend/forensic/models/manifest.json`; licence texts
in `backend/forensic/models/licences/`. The super-resolution weights were converted from the
authors' TensorFlow graphs by `backend/tools/convert_sr_models.py`, which pins the source file
hashes and copies the weights unchanged.

`python -m forensic.models list` lists them; `python -m forensic.models verify` re-hashes them.

## Generative steps

A filter is *generative* when its output can contain detail the camera never recorded
(today: `ai.super_resolution`). The code enforces:

- Output is watermarked "ILLUSTRATIVE ONLY" wherever it is stored, exported or printed; the UI
  shows the same banner over the live preview.
- Measurements on a chain with a generative step are refused.
- The step needs a written justification (at least 20 characters) from an examiner. It is
  logged (`chain.generative_justification`) and printed in the report. Without it the step
  cannot be accepted and the chain cannot be exported (`/api/chains/{id}/export.png` returns 403).
- Agents can propose a generative step but cannot write or change a justification; any
  agent edit to it is reverted.
- The reviewer agent flags every generative step: justified ones as a point the report must
  explain, unjustified ones as a failure.

## Licence policy

Only models whose licence allows commercial use and redistribution are bundled. Published
AI-image and deepfake detectors were reviewed and none is bundled: the strong ones carry
non-commercial or research-only licences or have not been validated on current generators.
`synthetic.learned` is a slot for a detector the lab licenses and validates itself.

## Installing a lab detector

```bash
python -m forensic.models install detector.onnx --card detector.json
```

The model goes into `FORENSIC_MODELS_DIR` (default `$FORENSIC_DATA_DIR/models`) with its SHA-256
pinned. The card must state:

```json
{
  "id": "lab.ufd_2025",
  "kind": "detector",
  "task": "Probability that an image is AI-generated",
  "architecture": "CLIP ViT-L/14 linear probe",
  "reference": "Ojha, Li and Lee, CVPR 2023",
  "source": "Where the weights came from, with the file's original hash",
  "licence": "The licence the lab holds",
  "training_data": "What it was trained on",
  "trained_on": "2025-03-01",
  "version": "1.0",
  "input": {"size": 224, "mode": "center_crop", "mean": [0.481, 0.458, 0.408],
            "std": [0.269, 0.261, 0.276], "scale": 0.00392157, "color": "RGB", "layout": "NCHW"},
  "output": {"activation": "sigmoid", "index": 0, "positive": "synthetic"},
  "threshold": 0.5,
  "validation": "Where and how the lab measured its accuracy"
}
```

`input.mode` is `center_crop`, `resize` or `tiles` (up to 36 tiles, scored separately and
mapped). `output.activation` is `sigmoid`, `softmax` (with `index` of the synthetic class) or
`none`. Once installed, "Learned AI-image detector" becomes available in the Authenticate tab.
The training date and version are printed with every result, because detectors age as
generators change.
