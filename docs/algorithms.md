# Algorithms in Forensic Workbench, and why they were chosen

This document describes every algorithm the workbench runs: what it does, the parameters
an examiner controls, the published method it follows, and why it was chosen over the
alternatives. It is written for examiners who must explain a result in a report or in
court, for reviewers checking that work, and for developers changing the code.

It describes version 0.3 as it is on `main`. Where a value is quoted (a threshold, a
default, a window size), it is the value in the code; the file is named so you can check.
The companion [user manual](user-manual.md) explains how to operate the software.

Contents

1. [Principles behind every choice](#1-principles-behind-every-choice)
2. [Evidence integrity: hashing, storage and the audit log](#2-evidence-integrity-hashing-storage-and-the-audit-log)
3. [Decoding images and video](#3-decoding-images-and-video)
4. [The processing chain engine](#4-the-processing-chain-engine)
5. [Enhancement: exposure and colour](#5-enhancement-exposure-and-colour)
6. [Enhancement: geometry](#6-enhancement-geometry)
7. [Enhancement: noise reduction and sharpening](#7-enhancement-noise-reduction-and-sharpening)
8. [Enhancement: deblurring](#8-enhancement-deblurring)
9. [Enhancement: multi-frame methods](#9-enhancement-multi-frame-methods)
10. [AI-assisted steps](#10-ai-assisted-steps)
11. [Authentication: file structure and metadata](#11-authentication-file-structure-and-metadata)
12. [Authentication: JPEG compression analyses](#12-authentication-jpeg-compression-analyses)
13. [Camera identification (PRNU)](#13-camera-identification-prnu)
14. [Synthetic media: provenance, camera traces, learned detectors](#14-synthetic-media-provenance-camera-traces-learned-detectors)
15. [Face-swap and face-edit checks](#15-face-swap-and-face-edit-checks)
16. [Video authentication](#16-video-authentication)
17. [Measurement: height, distance, speed](#17-measurement-height-distance-speed)
18. [Recorder (DVR) exports](#18-recorder-dvr-exports)
19. [Built-in agents: triage, planner, reviewer](#19-built-in-agents-triage-planner-reviewer)
20. [How detector accuracy is measured](#20-how-detector-accuracy-is-measured)
21. [Performance, determinism and the job queue](#21-performance-determinism-and-the-job-queue)
22. [References](#22-references)

---

## 1. Principles behind every choice

Six rules decided which algorithm went in where several could have. They recur
throughout the document, so they are stated once here.

**1. Reproducible to the bit.** A second examiner, or the same examiner a year later,
must get exactly the same pixels from the same original and the same chain. Every filter
is a pure function of its input and parameters; arithmetic that could vary with thread
scheduling (multi-threaded OpenCV, GPU kernels, parallel ONNX inference) is switched off;
random sampling uses fixed seeds. This rules out methods whose output depends on random
initialisation or on hardware, and it is why the report can say "reproduced bit for bit".

**2. Published, citable and explainable.** Each step must be explainable to a court in a
paragraph and traceable to a peer-reviewed paper or a textbook. ASTM E2825, SWGDE and the
former SWGIT guidelines all ask that a method be documented, repeatable and generally
accepted. Every filter and analysis carries its reference, which is printed in the report.
Between two methods of similar quality, the one an expert witness can describe from first
principles wins.

**3. Classical first, AI second.** Model-based methods recover only what the camera
recorded. Learned methods can predict detail that was never there. Learned steps are
allowed only where a classical method cannot do the job, are labelled "AI" everywhere, and
those that can invent detail ("generative") are walled off (section 10).

**4. "Consistent with", never "proves".** An authentication method measures a trace that
a manipulation usually leaves. Its absence does not prove authenticity and its presence
has innocent explanations. Every analysis reports what it measured, the threshold, its
validated accuracy and a reading guide, and no single score is presented as a verdict.

**5. Offline and licence-clean.** The workbench runs on air-gapped lab machines and is
meant to be used commercially. Methods that need a network service, or published code and
weights that carry research-only or non-commercial licences, are not bundled.

**6. Fast enough on a workstation CPU.** At the time of this version every algorithm
runs on the CPU. Where a faster approximate method and an exact one exist, the exact one
was chosen if it stayed interactive on a typical image. Section 21 describes the cost of
this choice and what is being changed.

---

## 2. Evidence integrity: hashing, storage and the audit log

### 2.1 Hashing at ingest (`forensic/store.py`)

**What it does.** While a file is copied into the store it is read once in 1 MB chunks and
fed to SHA-256 and MD5 at the same time. The copy is saved under its SHA-256 name and its
permissions are set to read-only. "Verify original" re-reads the stored file and compares
both the hash and the read-only flag.

**Why SHA-256 and MD5.** SHA-256 is the integrity hash: no practical collision is known
and it is the hash NIST and most forensic standards now recommend. MD5 is broken for
collision resistance but is still what many lab systems, disclosure schedules and older
tools quote, so it is recorded too, for cross-reference only. SHA-1 was not used: it is broken and adds nothing MD5 does not for
cross-reference. Computing both in one pass costs nothing extra in disk reads.

**Why content addressing.** Storing a file under its own hash means two copies of the same
evidence are stored once, a stored file cannot be silently replaced without its name no
longer matching its content, and every derived output can name exactly what it came from.

### 2.2 Write-once originals and derived outputs

Originals are never opened for writing after ingest. Every result (a rendered chain, an
analysis map, a converted recorder export) is a new, content-addressed file in `derived/`
or a new evidence item linked to its source. This is the "work on a copy" rule of every
forensic guideline, enforced by the code rather than by procedure.

Removing an evidence item or a chain from a case is a logged hide, not a deletion. The row
gets a `removed` record (who, when, an optional reason of up to 500 characters), the audit
log gets an `evidence.remove` or `chain.remove` entry, working copies and chains of a removed
item are hidden with it, and the stored original is untouched. **Restore** clears the record
and logs `evidence.restore` or `chain.restore` (a chain of a removed item comes back only with
its item). Only a human examiner can do either. A real delete was rejected
because the case record must still show everything that was received, including what was
later set aside and why; the report lists removed items in its evidence section.

### 2.3 The hash-chained audit log (`forensic/audit.py`)

**What it does.** Every action (ingest, chain edit, render, analysis, measurement, export,
review, note) is appended as a row containing the actor, the action, its parameters, the
hash of its output and the hash of the previous row. The row's own hash is the SHA-256 of
the canonical JSON of all those fields. The first row chains to 64 zeros.
`verify()` recomputes every hash from the start; editing or deleting any row breaks the
chain at that row and the Audit tab says where.

**Alternatives considered.**

| Option | Why not chosen (for now) |
|---|---|
| Plain database table with "no delete" permissions | Anyone with file access to the database can still edit rows undetected. |
| Database triggers that reject updates | Same weakness; also tied to one database engine. |
| A Merkle tree or blockchain | Detects the same tampering as a linear hash chain for a single-writer log, with far more machinery. |
| RFC 3161 trusted timestamps | Needs a timestamping authority, which an offline lab does not have. It can be added on top of the chain head later. |

A linear hash chain is the simplest structure that makes tampering evident, it verifies in
one pass, and it is easy to explain: "each entry contains a fingerprint of the one before".
It does not stop someone who can rewrite the whole log from rebuilding a consistent chain.
The countermeasure is to keep a copy of the latest entry's hash outside the system (in the
case file, or in a report already disclosed), so a later rewrite contradicts it.

### 2.4 Pixel hashes and chain hashes (`forensic/graph.py`)

A chain's result is identified by the SHA-256 of its 8-bit output pixels plus the array's
shape: exactly what an examiner sees and exports, independent of the PNG encoder. The chain
itself is identified by the SHA-256 of its normalised JSON (source file hash, every
filter's id, version and parameters). Hashing pixels rather than files means a result can be
re-checked even if the PNG library later changes its compression.

---

## 3. Decoding images and video

### 3.1 Still images (`forensic/media.py`)

Images are decoded by OpenCV, with Pillow as the fallback for GIF and WebP variants OpenCV
cannot read. Two common conveniences are deliberately **off**:

- **No EXIF auto-rotation.** The pixels are shown as stored. A rotation flag is a
  metadata claim that can be wrong or edited, and rotating silently would change the pixel
  grid that the JPEG analyses depend on. Rotate with the Rotate filter if needed; the step
  is then recorded.
- **No colour management.** Embedded ICC profiles are not applied, so pixel values in
  the inspector are the values in the file.

Pixels are converted to 32-bit floating point in the range 0 to 1 and every filter works in
that space; results are rounded back to 8 bits only for display, hashing and export. Working
in float avoids the rounding and clipping that accumulate when each step of an 8-bit chain
re-quantises.

### 3.2 Video

Video is decoded by FFmpeg. A frame is selected by its **decoded frame index**
(`select=between(n,start,end)` with pass-through timing), not by timestamp. Seeking by time
is approximate on long-GOP and variable-frame-rate recordings, which are common in CCTV;
seeking by index always returns the same frame. Frame types (I, P, B), key flags,
timestamps and packet sizes come from `ffprobe -show_frames`.

**Self-contained evidence.** Playlist and concat formats (HLS, DASH, concat lists) are
refused at ingest, because they make FFmpeg read other files: the decoded pixels would not
come from the hashed original.

**Why FFmpeg.** It decodes almost every codec and container in use, including several
proprietary DVR formats, it is open source, and its exact version is recorded in every
report so a decode can be repeated. Commercial SDKs from DVR vendors were not used: they
are closed, platform-specific, and their output cannot be independently checked.

---

## 4. The processing chain engine

**What it does** (`forensic/graph.py`). An enhancement is a chain: a source (one evidence
file, optionally a frame or a frame range up to 600 frames) followed by an ordered list of
steps. Each step names a filter id, the filter's version and its parameters. Before running,
the chain is normalised: unknown parameters are rejected, defaults filled in, values
range-checked. A step pinned to a filter version this build does not provide is refused,
because re-running it would not reproduce the original result.

Single-image filters applied to a frame range run on every frame. Multi-frame filters
consume the whole range and return either a range (stabilise) or one image (integrate,
super resolution, pick frame).

**Caching.** Intermediate results are kept in a small memory cache (24 entries, entries
larger than 512 MB are not cached) keyed by the hash of the chain up to that step. Changing
step 5 re-uses steps 1 to 4. The cache is never used for "Verify reproducibility" or by the
reviewer, which always re-run from the original.

**Live preview versus render.** The live preview runs the same engine but is not stored or
audited and is never evidential. "Render result" runs the saved chain from the original,
stores lossless PNGs and records the output hash in the audit log.

**Why a linear chain.** It is what an examiner reads in a report: step 1, step 2, step 3.
Node graphs with branches (as in some commercial tools) are more flexible but harder to
read back. The document format allows branching later without breaking saved chains.

---

## 5. Enhancement: exposure and colour

All in `forensic/filters/exposure.py`. These are pointwise or near-pointwise operations that
change how values are displayed, not where structures are.

### 5.1 Levels

Maps the input range [black, white] linearly onto the output range, with a gamma curve in
between: `out = out_black + ((in − in_black)/(in_white − in_black))^(1/gamma) × (out_white − out_black)`.
Values outside the input range clip. *Why:* it is the standard, fully described tone
operation in every image tool and textbook (Gonzalez and Woods, ch. 3); gamma above 1 lifts
mid-tones without moving black or white.

### 5.2 Brightness and contrast

`out = (in − 0.5) × contrast + 0.5 + brightness`, clipped. Contrast scales around mid-grey so
a contrast change does not also brighten or darken the image overall.

### 5.3 Curves (monotone cubic)

A tone curve through examiner-set control points, interpolated with **PCHIP**
(Fritsch-Carlson monotone piecewise cubic), applied through a 4096-entry lookup table, to all
channels or one.

*Why PCHIP rather than a natural cubic spline:* an ordinary cubic spline can overshoot
between control points and become non-monotone, which would swap the order of two tones
(a darker object becoming lighter than a brighter one). That is a content change, not an
enhancement. PCHIP guarantees the curve never reverses, while staying smooth. Straight-line
segments (as in some tools) are monotone too but create visible kinks in gradients.

### 5.4 Local contrast (CLAHE)

Contrast-limited adaptive histogram equalisation (Zuiderveld 1994) applied to the
**lightness channel of CIE L\*a\*b\*** only. The image is split into tiles (default 8×8); each
tile's histogram is clipped at `clip_limit` times the average and redistributed, then
equalised; tile results are bilinearly blended.

*Why CLAHE:* it brings out detail in shadows and highlights at the same time, which a global
curve cannot. The clip limit stops it amplifying noise in flat areas, the main failure of
plain adaptive equalisation.
*Why on L\* only:* equalising R, G and B separately shifts hues; equalising lightness keeps
colours. *Alternatives:* Retinex methods (MSR, MSRCR) give similar results but have more
parameters and a less direct explanation; local Laplacian filtering is better at avoiding
halos but much slower and harder to describe; learned low-light enhancers (Zero-DCE and
similar) are not used because their output is not a documented function of the input.

### 5.5 Histogram equalisation

Global equalisation of L\*. Kept as the simple reference method; CLAHE is usually better.

### 5.6 Colour balance

Multiplies each channel by a gain. In "grey world" mode the gains make the image's mean
colour neutral (Buchsbaum 1980). *Why grey world:* it is the classic, one-line automatic
white balance; it fails on scenes dominated by one colour, which is why manual gains are
also offered. Grey-edge and learned colour-constancy methods were not added: they do
better on benchmark scenes, but CCTV colour casts usually come from the camera's own
white balance, which the examiner can correct by eye against a known neutral object.

### 5.7 Greyscale or channel, and invert

Greyscale uses **Rec. 601 luma** (0.299 R + 0.587 G + 0.114 B), the weighting of the JPEG
and most CCTV pipelines, so the result matches the brightness the codec itself worked with.
Isolating one channel helps when one channel carries the detail (for example red for a
number plate under sodium light). Invert is `1 − v`.

---

## 6. Enhancement: geometry

All in `forensic/filters/geometry.py`.

### 6.1 Interpolation, used by every resampling step

Rotate, resize, aspect ratio, perspective and lens correction each let the examiner choose
the interpolation: **nearest**, **bilinear**, **bicubic** (default) or **Lanczos (8×8)**.

- Nearest neighbour keeps original pixel values; use it when the result will be inspected
  pixel by pixel or measured.
- Bicubic is cubic convolution (Keys 1981; OpenCV uses the coefficient a = −0.75). It is the
  accepted default: sharper than bilinear without the ringing of Lanczos.
- Lanczos is sharpest but rings around high-contrast edges.

No interpolation adds detail; the descriptions in the app say so.

### 6.2 Crop and flip

Exact pixel copies; no resampling and no reference needed. Crop takes x, y, width and height
in pixels (0 for width or height means "to the edge").

### 6.3 Rotate

Rotations by 90, 180 and 270 degrees are exact array moves. Other angles are resampled about
the centre and the canvas is enlarged so no pixel is cut off.

### 6.4 Resize and aspect-ratio correction

Resize scales by independent horizontal and vertical factors (0.05 to 16). Aspect-ratio
correction rescales the width so the frame has the given display ratio; CCTV is often
stored with non-square pixels (for example 704×576 shown at 4:3), and measuring or
comparing faces without correcting it distorts proportions.

### 6.5 Perspective correction

The examiner clicks four corners (top-left, top-right, bottom-right, bottom-left) of a planar
object such as a plate or a document. The four point pairs define a **planar homography**
(Hartley and Zisserman, ch. 2 and 4), computed exactly from four points, and the image is
warped so the quadrilateral becomes a rectangle. The output size is the longer of each pair
of opposite sides unless given.

*Why a homography:* a plane seen by a pinhole camera is related to its front-on view by
exactly this transform, so the correction is geometrically exact for a flat object (it is
not for a curved one, such as a bent plate).

### 6.6 Lens distortion

Removes barrel and pincushion distortion with the **Brown-Conrady model** (Brown 1966, as
used in Zhang's 2000 calibration method): three radial coefficients k1 to k3 and two
tangential p1, p2, with the focal length given as a multiple of the image's larger side and
the principal point at the centre.

*Why Brown-Conrady:* it is the model OpenCV, MATLAB and photogrammetry software use, so
coefficients from a calibration of the same camera can be typed in directly. The division
model (Fitzgibbon) handles very strong fisheye distortion with fewer terms, but few CCTV
cameras need it and its coefficients cannot be exchanged with calibration tools.
Automatic estimation from straight lines (plumb-line methods) is a candidate for a later
version; for now the examiner adjusts k1 until straight edges look straight.

### 6.7 Deinterlace

Interlaced video stores two half-frames (fields) captured at different moments. Three
modes:

- **Field** keeps one field and fills the missing lines by averaging the lines above and
  below (line averaging). Every output frame then shows a single moment in time.
- **Blend** averages adjacent lines; smooth, but mixes two moments.
- **Split** stacks the two fields one above the other for inspection.

*Why line averaging and not a motion-adaptive deinterlacer (YADIF, BWDIF):* motion-adaptive
methods look at neighbouring frames and choose, pixel by pixel, whether to take a line from
another field. That gives a nicer picture but mixes information from different moments in a
way that depends on a motion detector's thresholds. For evidence, a frame that is one moment
in time, with interpolation that is trivial to explain, is the safer default (de Haan and
Bellers 1998 compare the families).

---

## 7. Enhancement: noise reduction and sharpening

All in `forensic/filters/denoise.py`.

### 7.1 Median filter

Replaces each pixel by the median of its square neighbourhood (3 to 15 pixels). Removes
impulse ("salt and pepper") noise and dropped-pixel speckle while keeping edges (Tukey 1977).

### 7.2 Gaussian blur

Smooths with a Gaussian of the given sigma. Included mainly as a building block and for
reducing compression blocking before other steps.

### 7.3 Bilateral filter

Averages only neighbours that are close in both position and colour (Tomasi and Manduchi
1998), so it smooths flat areas while keeping edges. Fast and easy to explain; on heavy
noise it leaves a "cartoon" look, which is why non-local means is also offered.

### 7.4 Non-local means

For each pixel, averages pixels anywhere in a search window (default 21×21) whose surrounding
patch (default 7×7) looks similar, weighted by similarity (Buades, Coll and Morel 2005). The
OpenCV implementation used works in CIE L\*a\*b\* with separate strengths for lightness and
colour.

*Why NLM:* it is the strongest classical denoiser with a short, intuitive explanation
("average pixels whose neighbourhoods look alike"). *Alternatives:*

| Method | Assessment |
|---|---|
| BM3D (Dabov et al. 2007) | Usually 0.5 to 1 dB better than NLM on Gaussian noise, but slower, with more internal stages to explain, and its reference implementation carries a non-commercial licence. A clean-room implementation is a candidate for a later version. |
| Learned denoisers (DnCNN, FFDNet, NAFNet, Restormer) | Better on benchmarks, but they learn priors from training images and can remove or invent fine texture; treated like any other AI step, so not used as the default. |
| Wavelet thresholding | Simpler but clearly weaker on natural images. |

### 7.5 Periodic noise removal (FFT notch)

Interference patterns, scan lines and screen moiré are sharp peaks in the 2-D Fourier
spectrum. The filter takes the log-magnitude spectrum of the luma, divides it by a 5×5
median-filtered background, and marks as peaks the points more than `threshold` times the
median deviation above it, outside a protected centre (default 5 % of the spectrum, which
holds the image's real structure). Each peak is suppressed with a Gaussian notch, and the
same mask is applied to all three channels (Gonzalez and Woods, sec. 5.4).

*Why automatic peak finding:* manual notch painting (as in many tools) is subjective and hard
to record. Finding peaks by a stated threshold makes the step repeatable from its parameters.

### 7.6 Unsharp mask

Adds back `amount × (image − Gaussian-blurred image)`, optionally only where the difference
exceeds a threshold. It raises edge contrast; it does not recover detail and strong settings
create halos, as the app states. *Why:* it is the universal, transparent sharpening operator.
When the cause of softness is known (motion, defocus), deconvolution (section 8) is the
correct tool instead.

---

## 8. Enhancement: deblurring

All in `forensic/filters/deblur.py`. Both methods invert a **known** blur described by a
point-spread function (PSF). All arithmetic is NumPy FFT in 64-bit float, which is
deterministic.

### 8.1 The blur models

- **Motion**: a straight line of a given length and angle, drawn with 16 samples per pixel
  and bilinear splatting so non-integer lengths and angles are smooth.
- **Defocus**: a uniform disk of a given radius, anti-aliased by 8×8 supersampling.
- **Gaussian**: general softness of a given sigma.

The image is padded symmetrically by the PSF size before filtering so the FFT's wrap-around
does not ring at the borders.

### 8.2 Wiener deconvolution

`F = conj(H) / (|H|² + NSR) × G` in the frequency domain, where H is the PSF's transform
and NSR the noise-to-signal ratio (Wiener 1949; Gonzalez and Woods, sec. 5.8). NSR trades
sharpness for noise and ringing: raise it if the result looks grainy or rippled.

*Why:* it is the optimal linear inverse for a known blur and Gaussian noise, it is instant,
and it has one interpretable parameter. Plain inverse filtering (`G/H`) explodes at the
spectrum's zeros and is unusable on real footage.

### 8.3 Richardson-Lucy, with optional total variation

Iterative maximum-likelihood deconvolution for Poisson (photon) noise (Richardson 1972; Lucy
1974): each iteration re-blurs the estimate, compares it with the observed image and
corrects by the ratio. It keeps values positive and copes with low-light noise better than
Wiener. More iterations sharpen more and amplify noise; the optional total-variation term
(Dey et al. 2006) damps that noise while keeping edges.

*Why both Wiener and RL:* they fail differently. Wiener is fast and predictable; RL handles
dark, noisy footage better. Offering both, with the planner trying both, lets the examiner
compare.

### 8.4 Estimating motion blur (cepstrum)

"Estimate motion blur" suggests a length and angle for the motion PSF. A uniform linear blur
of length L puts periodic zeros in the spectrum, which appear in the **cepstrum** (inverse
FFT of the log spectrum) as a strong negative peak at distance L along the blur direction.
The code windows the luma (Hann), computes the cepstrum, and searches every angle from 0 to
179 degrees in 1-degree steps and every length from 3 to 60 pixels for the deepest value.
The strength reported is that depth in standard deviations; below about 4 means no clear
linear blur. The estimate is a suggestion, never applied automatically.

**Alternatives to known-PSF deconvolution.**

| Approach | Why not used |
|---|---|
| Blind deconvolution (Fergus 2006, Levin 2009, Krishnan 2011) | Estimates the PSF and the image together. Results depend on initialisation and regularisation, are not unique, and are hard to defend: two runs can disagree. The cepstral estimate plus a known-PSF method gives the same benefit with a PSF the examiner can state. |
| Learned deblurring (DeblurGAN, MPRNet, NAFNet) | Can produce convincing but invented edges and characters; unacceptable for plates and faces without the generative-step controls. |
| Radon-transform blur estimation | Works on the same spectral zeros as the cepstrum; the cepstrum gives length and angle in one search and is simpler to explain. |

---

## 9. Enhancement: multi-frame methods

All in `forensic/filters/multiframe.py`. They need a frame range on the chain's source.

### 9.1 Stabilise / register frames (ECC)

Aligns every frame to a reference frame by maximising the **enhanced correlation
coefficient** (Evangelidis and Psarakis 2008), optionally on a region of interest only (a
plate, a face). Motion models: translation, Euclidean (shift and rotation, default), affine,
homography. Defaults: up to 200 iterations, convergence epsilon 10⁻⁶. Frames that fail to
converge are left as they were and reported, never forced.

*Why ECC:*

| Method | Assessment |
|---|---|
| ECC (chosen) | Sub-pixel accurate, uses every pixel in the region (robust on low-texture CCTV where feature detectors find few points), invariant to brightness and contrast changes between frames, deterministic. |
| Feature matching (ORB, SIFT, AKAZE + RANSAC) | Fast and robust to large motion, but RANSAC is random (needs a fixed seed to reproduce) and small, blurry, compressed regions often have too few features. |
| Phase correlation | Very fast and exact for pure translation, but does not handle rotation or perspective without extra steps. |
| Dense optical flow | Handles non-rigid motion but warps each pixel independently, which can move content in ways that are hard to justify. |

### 9.2 Frame integration

Combines aligned frames into one image: **mean** (noise falls with the square root of the
number of frames), **median** (robust to a passing object), or **sigma-clipped mean** (per
pixel, frames further than `sigma` standard deviations from the median are ignored, then the
rest averaged). Sigma clipping is standard in astronomical stacking and is recommended by the
SWGIT guidelines; it keeps most of the mean's noise reduction while rejecting outliers.

### 9.3 Pick frame

Takes one frame out of a range, for example after stabilisation. Exact copy.

### 9.4 Multi-frame super resolution

Builds one higher-resolution image (2×, 3× or 4×) from several frames that sample the scene
at different sub-pixel offsets (a slowly moving car, a hand-held camera).

1. **Register** every frame to the reference with ECC (affine by default, up to 300
   iterations, epsilon 10⁻⁷), optionally on a region. Frames that cannot be registered are
   dropped, because they would only add blur.
2. **Shift-and-add start**: each frame is warped onto the fine grid (bicubic) and the frames
   are combined by median (robust) or mean.
3. **Iterative back-projection** (Irani and Peleg 1991): the current estimate is blurred by a
   Gaussian camera PSF (default sigma 0.8 low-resolution pixels), sampled back into each
   frame's geometry, compared with that frame, and the errors are mapped back to the fine grid,
   combined (median when robust, Farsiu et al. 2004) and added with a step size. 15 iterations
   by default.

*Why this method:* it is model-based. The result is the image that, blurred and sampled like
the camera did, agrees with every observed frame. It cannot add detail that is in none of the
frames, and the median makes one bad frame harmless. *Alternatives:* full Farsiu
bilateral-total-variation regularisation gives slightly cleaner edges but adds a regulariser
that shapes the result; learned video super resolution (BasicVSR, EDVR, VRT) is much more
impressive on benchmarks and generative, so it would be walled off like single-image AI super
resolution and is not used.

---

## 10. AI-assisted steps

All learned models are listed in `forensic/models/manifest.json` with the SHA-256 of their
weights, source, licence, training data and limits. Weights are verified before every load;
a mismatched file is refused. Inference runs on ONNX Runtime on the CPU, single-threaded, so
results are bit-identical on the same CPU family. See also [ai-models.md](ai-models.md).

### 10.1 AI super resolution (single image) — generative

Enlarges one image 2×, 3× or 4× with **FSRCNN** (Dong et al. 2016) or **ESPCN** (Shi et al.
2016). The network runs on the brightness channel (Y of YCrCb); colour is enlarged bicubically,
as in the original papers. Large images are processed in 384-pixel tiles with a 16-pixel margin,
wider than either network's receptive field, so tiling does not change the result. The output
is limited to 8192 pixels per side.

*Why these models:* they are small (35 to 100 KB), run quickly and deterministically on a CPU,
are well documented, and carry the Apache-2.0 licence. *Why not Real-ESRGAN, SwinIR, HAT or
diffusion upscalers:* they produce far more convincing detail precisely because they invent
more of it; the forensic risk grows with the realism. Even the small models predict detail, so
the step is **generative**:

- the result is watermarked "ILLUSTRATIVE ONLY" wherever it is stored, exported or printed,
  and the viewer shows the same banner;
- measurements on a chain containing it are refused;
- it cannot be accepted or exported until an examiner writes a justification of at least
  20 characters, which is logged and printed in the report; agents cannot write it;
- the reviewer agent flags it in every review.

For evidential enlargement use multi-frame super resolution (9.4).

### 10.2 Face redaction (YuNet)

Finds faces with **YuNet** (Wu, Peng and Yu 2023) through OpenCV's wrapper (score threshold
0.6, non-maximum suppression 0.3) and pixelates, blurs or blanks each box plus a margin, for
disclosure copies. Missed faces can be added as manual regions.

*Why YuNet:* about 75 000 parameters, MIT licence, fast on a CPU, and accurate enough on
frontal and moderately turned faces. *Alternatives:* Haar cascades (Viola-Jones) are free and
tiny but miss far more faces; RetinaFace and SCRFD are more accurate on small faces, but the
widely distributed weights (InsightFace) are licensed for non-commercial research only.
Redaction is not generative (it removes information), so it needs no justification, but the
app reminds the examiner to check every frame before release.

---

## 11. Authentication: file structure and metadata

`forensic/analysis/metadata.py` and `forensic/analysis/jpeg.py`.

### 11.1 Still images

- **EXIF, GPS, XMP**: read with Pillow from all IFDs (main, Exif, GPS, Interop, thumbnail);
  GPS converted to decimal degrees. Binary blobs are summarised, and all text read from the
  file is presented as the file's own content (never interpreted).
- **Consistency checks** that raise a warning: no EXIF at all; a Software tag naming an editor
  (Photoshop, GIMP, Lightroom, Snapseed, PicsArt, Affinity, Paint); modification time different
  from capture time; EXIF dimensions different from the image; XMP editing history; an embedded
  thumbnail whose aspect ratio differs from the image by more than 2 % (a sign of cropping
  after capture) or that cannot be decoded; irregular JPEG structure; bytes after the
  end-of-image marker.
- **JPEG marker walk**: the file is parsed as untrusted input; every segment length is bounds
  checked and the walk stops at the first inconsistency rather than guessing. It lists every
  marker with its offset, reads the quantisation tables (DQT) in natural order, the frame
  header (size, components, chroma subsampling) and comments.
- **Quantisation table match**: the luminance and chrominance tables are compared with the
  libjpeg (IJG) scaling of the ITU-T T.81 Annex K tables at every quality from 1 to 100. An
  exact match says "consistent with a libjpeg-family encoder at quality N", common in software
  and apps; no exact match points to camera firmware or other software. The closest quality and
  the mean difference are reported.

*Why rule-based checks:* each warning is a specific, checkable inconsistency that an
examiner can explain in a sentence, which is what SWGDE's image authentication guideline asks
for. *Not yet included:* a database of quantisation tables per camera model (as in some
commercial tools' "JPEG signature" feature), which needs a licensed or lab-built database.

### 11.2 Video

Container and stream data from ffprobe, the per-frame I/P/B sequence, GOP lengths, encoder
tags and the field order. Warnings for timestamp gaps larger than 1.9 times the typical frame
interval and for timestamps that do not increase; information for irregular GOP lengths and
interlacing.

---

## 12. Authentication: JPEG compression analyses

### 12.1 Error Level Analysis (ELA) — `forensic/analysis/compression.py`

Re-saves the image as JPEG at a known quality (default 90, baseline, 4:4:4, libjpeg standard
tables), takes the absolute difference with the original and amplifies it (default ×20). It
also measures the error per 8×8 block and reports the share of blocks more than 6 robust
deviations from the median.

*Why included, and its limits:* ELA (Krawetz 2007) is widely known and quick, so examiners
expect it. It is strongly content-dependent (edges and texture are bright in authentic
images too) and its reading guide says so. It is never used by triage; the double-JPEG
analyses below are the statistically grounded versions of the same idea.

### 12.2 JPEG Ghost

Re-saves the image at a range of qualities (default 50 to 95 in steps of 5), computes the
squared difference averaged over 16×16 blocks at each, and shows a montage of normalised maps.
A region that was once compressed at a lower quality than the rest shows a dark "ghost" at
that quality (Farid 2009). Local minima of the global difference curve suggest earlier save
qualities.

### 12.3 Aligned double JPEG (A-DJPG) — `forensic/analysis/double_jpeg.py`

When a JPEG is decoded and saved again on the same 8×8 grid, each DCT coefficient's
histogram carries a periodic pattern left by the first quantisation step. The method follows
Bianchi and Piva (2012), with ideas from Lin et al. (2009):

1. Read the decoded luma plane directly in YCbCr (no colour conversion, which would blur the
   trace) and take the 8×8 DCT on the JPEG grid, divided by the file's own quantisation table.
2. Take the same DCT on a grid shifted by (4, 4). Off-grid coefficients carry no quantisation
   trace, so their histogram serves as the model of a **singly** compressed image.
3. For each of the first 15 AC frequencies in zig-zag order, fit a mixture: a share alpha of
   coefficients follows the double-compression model for a first step q1 (1 to 40), the rest
   the single model. The fit maximises the likelihood over q1 and alpha.
4. A frequency "supports" double compression when alpha ≥ 0.3 and the log-likelihood gain is
   above 0.002 per coefficient. The global score is the sum of gain × alpha over supporting
   frequencies; **detection at 0.05** with at least two supporting frequencies.
5. The first quality is estimated as the libjpeg quality whose table best matches the fitted
   q1 values (needs three supporting frequencies).
6. **Localisation**: for every 8×8 block, the log-likelihood ratios of its coefficients under the
   two models are summed over supporting frequencies, smoothed over 3×3 blocks and turned into
   a probability of single compression. In a re-saved image, a pasted region usually lacks the
   trace and is bright in the map. A warning is raised when 1 % to 40 % of blocks exceed 0.8.

*Why this method:* it gives a global test, a first-quality estimate and a per-block map from
one statistical model, its assumptions are stated (aligned grid, first quality lower than the
second), and its accuracy is measured on the validation corpus (section 20). It fails when
the first quality was higher than the second, and after resizing or off-grid cropping, for
which the next analysis exists.

### 12.4 Non-aligned JPEG grid (NA-DJPG)

When a JPEG is cropped, or a region pasted off-grid, and saved again, the first compression's
8×8 grid survives at an offset from the final grid.

1. Take the 8×8 DCT at all 64 grid offsets and measure, per block, the mean absolute value
   of mid and high frequencies (u + v ≥ 4). A grid on which the image was once quantised
   pushes many of those coefficients to zero, so its energy dips.
2. Offsets in the same row or column as the final grid dip in authentic images too. An
   additive row-plus-column model of log energy (median polish, ignoring the final grid at
   (0, 0)) is removed, so a genuine first-compression grid stands out as an isolated dip.
3. The dip's robust z-score is compared with a threshold of 10 for off-axis offsets and 30 for
   on-axis ones (calibrated on the validation corpus); a score of 1 or more means detected.
4. The same test in sliding windows (default 96 pixels) gives a map, so a secondary grid
   confined to one region can be seen.

*References:* Luo et al. (2007) for blocking-artefact grids, Bianchi and Piva (2012) for
non-aligned double compression. *Why this formulation:* it needs no training, its decision
statistic is a single, explainable number, and the median polish removes the main source of
false alarms in simpler blockiness measures.

**Learned alternatives to 12.1 to 12.4.** Splicebuster, Noiseprint, ManTra-Net, CAT-Net and
TruFor localise manipulations across many kinds of edit, often better than the classical
methods on benchmarks. They were not bundled because the published code and weights reviewed
carry research-only or non-commercial licences, and their training data and failure modes are
harder to put in front of a court. A lab that licenses one can install it in the learned
detector slot (section 14.3).

---

## 13. Camera identification (PRNU)

`forensic/prnu.py` and `forensic/analysis/camera.py`. Every sensor has a faint, fixed,
multiplicative noise pattern K (photo-response non-uniformity): pixel i records
`I = I0 × (1 + K) + noise`. The implementation follows the Binghamton reference
(Lukáš, Fridrich and Goljan 2006; Goljan, Fridrich and Filler 2009).

**Noise residual.** Each colour channel (0 to 255) is denoised with the **Mihçak wavelet
denoiser**: a 4-level Daubechies-8 wavelet transform; in each detail band the local signal
variance is estimated in 3, 5, 7 and 9-pixel windows, the minimum is taken, and the
coefficients are Wiener-shrunk assuming noise sigma 3. The residual is the part the denoiser
removes. The three channel residuals are combined into luma, and **zero-mean total** removes
row and column means on each of the four 2×2 colour-filter sub-lattices (which hold
demosaicing artefacts common to all cameras of a model).

**Fingerprint.** From N images of the camera, `K = Σ(W × I) / Σ(I²)`, the maximum-likelihood
estimate, excluding saturated pixels (above 250). The estimate is zero-meaned again and
**Wiener-filtered in the DFT domain** to suppress periodic artefacts (JPEG blocking, CFA
interpolation) that are shared by other cameras. At least two images are required; 20 or more
flat, bright, unsaturated images are recommended.

**Matching.** The questioned image's residual times its intensity is cross-correlated with the
fingerprint by FFT. The score is the **peak-to-correlation energy (PCE)**: the squared peak
divided by the mean squared correlation outside an 11×11 neighbourhood of the peak. The search
covers the four 90-degree rotations, every crop position (when the image is smaller than the
fingerprint) and, optionally, down-scaling from 100 % to 50 % in 5 % steps. **Decision at
PCE 60**, the threshold from Goljan, Fridrich and Filler's large-scale test (about 1 false
alarm in 100 000 for full-size images). The number of hypotheses tried is reported, because
searching more positions makes chance matches more likely.

*Why this method:* it is the most validated camera-identification method in the literature,
has been accepted as evidence in courts, with a decision threshold from a test on over a million
images. *Alternatives:* learned camera-model classifiers identify the model, not the individual
device, and carry the usual training-data questions; Noiseprint is model-level and
non-commercially licensed. *Known limits* (printed in the reading guide): strong compression,
resizing and heavy processing weaken the fingerprint, and modern phones' computational
photography can weaken or fake a match, so validate on the same phone model.

---

## 14. Synthetic media: provenance, camera traces, learned detectors

`forensic/analysis/synthetic.py`.

### 14.1 Content credentials and AI provenance

- **C2PA manifests** (Content Credentials) are read and validated with the reference `c2pa`
  library, **offline**: no certificate revocation (OCSP) or remote manifest fetching. The
  validation checks that the signed hashes match the content and that the claims are intact.
  Signers are "trusted" only if the lab configures a trust list
  (`FORENSIC_C2PA_TRUST_ANCHORS`); otherwise a valid signature is reported as from an untrusted
  signer.
- **Declarations of AI generation**: the IPTC digital source type in C2PA actions and in XMP
  (`trainedAlgorithmicMedia`, `compositeWithTrainedAlgorithmicMedia` and others); PNG text
  chunks of the kind image generators write (`parameters`, `prompt`, `workflow` and others);
  names of generators in metadata fields (DALL-E, Midjourney, Stable Diffusion, Firefly, Imagen,
  Gemini, FLUX, Sora, Runway and others).

*Why:* a signed provenance record is the only way to *positively* establish where a file came
from, and an explicit AI-generation declaration is strong evidence. The absence of either means
nothing (most cameras write no credentials and platforms strip metadata); the reading guide
says so.

### 14.2 Camera-pipeline traces (CFA and resampling)

**CFA demosaicing traces.** A camera measures one colour per pixel through a Bayer filter and
interpolates the rest. In the green channel, half the pixels are measured and half
interpolated, so the interpolated half fits its four neighbours far better. The code predicts
each green value from its four neighbours, and in 32-pixel blocks compares the prediction
error on the two quincunx lattices as a log ratio (after Gallagher and Chen 2008, and Ferrara
et al. 2012 for the block map). The flattest quarter of blocks is ignored. Traces are
**present** when the median log ratio is at least 0.15 in magnitude and at least 75 % of blocks
agree in sign. A block map shows where the pattern is present or reversed.

**Resampling.** Scaling or rotating after capture makes neighbouring pixels periodically
correlated. Following Kirchner (2008), a fixed linear predictor is applied to the luma, its
residue is turned into a "p-map", and the p-map's spectrum is searched for peaks relative to a
median-filtered background (detection at 9 times background). For JPEG files, peaks on the
8-pixel grid frequencies are ignored because compression causes them. The centre 1024×1024 is
analysed on large images (periodicity is global).

*Why Kirchner's fixed predictor rather than Popescu and Farid's EM method:* the same detection
power on typical factors, without an iterative estimation whose result depends on
initialisation, and an order of magnitude faster.

*Limits:* both traces disappear after JPEG at quality 95 or below, after resizing, and in many
phone pipelines; the validation card gives the measured fall-off.

### 14.3 Learned AI-image detector (lab-installed slot)

No learned detector ships. A lab can install an ONNX model with a model card (training data,
training date, version, input preprocessing, output activation, threshold and validation); the
analysis then runs it in centre-crop, resize or tile mode (up to 36 tiles, mapped), and reports
the score, the threshold, the model's identity and weights hash, and its training date.

*Why a slot rather than a bundled model:* the detectors reviewed (for example Ojha et al. 2023,
Cozzolino et al. 2024) either carry non-commercial licences or have not been validated on the
generators current at release; detectors also age quickly as generators change. Making the lab
license, validate and record the model keeps the responsibility and the evidence trail clear.

---

## 15. Face-swap and face-edit checks

`forensic/analysis/faces.py`. A face swap or face edit pastes a face rendered by another
process into a host image. The pasted region typically differs from its surroundings in
sensor-noise level and fineness of detail, and in a re-saved JPEG it often lacks the image's
double-compression history.

1. **Find faces** with YuNet (score threshold 0.6) and/or regions the examiner marks.
2. **Measure** inside an ellipse at 0.8 of the face box and in a ring from 1.25 to 1.65 times
   the box:
   - *noise* by Immerkær's fast estimator (absolute response to a 3×3 Laplacian-difference
     kernel), taken from the flattest 60 % of pixels by gradient so texture does not inflate it;
   - *detail* as the mean absolute Laplacian (after a 0.8-pixel Gaussian), divided by the noise
     so a noisy region is not mistaken for a sharp one.
   The face is described by two log ratios, inside versus ring.
3. **Compare with the image itself**: the same measurement is taken at 200 random places of the
   same size (seeded, avoiding faces), and each log ratio becomes a robust z-score (median and
   MAD of those 200). A z of 4 or more is flagged: the face differs from its surroundings more
   than almost any other region of the image does.
4. **Compression history**: if the image passes the A-DJPG test (12.3), the share of the face's
   blocks that look singly compressed is compared with the whole image's. A face with half its
   blocks single-compressed in an image where at most a quarter are is flagged.
5. **Reliability by size**: faces under 12 pixels between the eyes are not assessed; under 40
   are reported as "limited".
6. **Video**: frames are sampled (default 25 frames, every 5th), faces tracked by box overlap
   (IoU ≥ 0.3), and a strong frame-to-frame fluctuation of the noise ratio (standard deviation
   above 0.35 over at least 5 frames) is flagged as consistent with per-frame face rendering.

*References:* Mahdian and Saic (2009) for noise inconsistency, Immerkær (1996) for the noise
estimator, Li et al. (2020, Face X-ray) for the blending boundary idea, Bianchi and Piva (2012)
for the compression history.

*Why a classical, self-calibrating test:* comparing the face with 200 places in the same image
means the image's own variability sets what counts as unusual, which works across cameras and
compression levels without training. *Alternatives:* learned deepfake detectors (XceptionNet
on FaceForensics++, EfficientNet ensembles from the DFDC challenge, newer CLIP-based detectors)
score higher on their benchmarks but generalise poorly to new generators and carry dataset
licence restrictions; one can be installed in the learned slot. *Known limits:* make-up, motion
blur, focus on the face and phone beautification also change noise and detail; modern swaps
can match noise. A small z is not evidence of authenticity.

---

## 16. Video authentication

`forensic/analysis/video.py`.

### 16.1 Double encoding (variation of prediction footprint)

When a video is decoded and re-encoded, frames that were intra-coded (I) in the first encoding
and become predicted (P) in the second are predicted unusually badly from their neighbours, so
the second encoder spends more intra macroblocks and fewer skipped ones on them (Vázquez-Padín
et al. 2012).

1. FFmpeg's `-debug mb_type` output gives each decoded frame's picture type and the count of
   intra and skipped macroblocks (single-threaded decode, up to 3000 frames by default).
2. For P-frames only, the intra share and the negative skip share are each turned into a robust
   z-score against the median of the 6 neighbouring P-frames on each side, and added.
3. P-frames scoring 4 or more (excluding the frame right after an I-frame) are candidate
   footprints. Detection needs at least 3 candidates with a regular spacing (60 % of gaps within
   25 % of the median gap). The median gap estimates the first encoding's GOP length.

Works on H.264 and MPEG-1/2/4 Part 2 streams, for which FFmpeg reports macroblock types. *Why
VPF:* it detects re-encoding and estimates the original GOP from information in the bitstream,
without decoding to pixels and comparing, and it is well cited. *Limits:* it fails with
B-frame-heavy or constant-quality second encodings, very static scenes, and when the second GOP
is a multiple of the first. Re-encoding alone is not manipulation; it says the file is not the
recorder's original stream. HEVC is not yet supported because FFmpeg does not expose its
coding-unit types the same way.

### 16.2 Frame duplication and deletion

1. Every frame is decoded to a 96-pixel-wide greyscale thumbnail (area scaling, which averages
   out compression noise).
2. The frame-to-frame change is the mean absolute difference of consecutive thumbnails.
3. **Repeated frames**: change below 15 % of the median change (and below 0.6 grey levels).
4. **Jumps**: change with a robust z-score of 8 or more against its 8 neighbours on each side,
   and more than twice the median. A jump can be a deletion, an insertion, a cut, a camera
   switch or a light turning on.
5. **Repeated segments**: runs of at least 5 frames that reappear later, compared by RMS
   difference under half the typical change, and only if the run contains motion (a static
   scene repeats trivially). This follows Wang and Farid (2007).
6. **Timestamp gaps**: intervals longer than 1.5 times the median frame interval.

*Why thumbnails and simple differences:* they make the test fast enough to scan tens of
thousands of frames, and each finding points the examiner to specific frames to look at,
rather than producing an opaque score. Optical-flow-based and learned inter-frame forgery
detectors exist; they are more sensitive to single-frame deletions in moving scenes but slower
and harder to explain, and CCTV's own frame drops would still dominate their false alarms.

---

## 17. Measurement: height, distance, speed

`forensic/measure.py`. Every result carries a **95 % interval** from Monte Carlo propagation:
the clicked points are perturbed by Gaussian noise of the stated clicking uncertainty (default
1 pixel), and for plane methods the surveyed coordinates by the stated survey uncertainty; the
measurement is recomputed 2000 times (400 for camera resection, which is slower) and the 2.5th
and 97.5th percentiles are reported. The random generator is seeded, so the interval is
reproduced exactly. If more than half the samples fail, the interval is reported as unknown,
which means the geometry is unstable.

*Why Monte Carlo rather than analytic error propagation:* the measurement functions are
strongly non-linear (vanishing points can move far for a small click change), and first-order
propagation underestimates the interval exactly when it matters. Monte Carlo makes no
linearity assumption and its method is simple to describe.

### 17.1 Height by single-view metrology

From one image and one object of known height standing on the same ground plane (Criminisi,
Reid and Zisserman 2000):

1. The examiner traces at least two lines that are **vertical** in the scene and two pairs of
   lines that are **parallel on the ground** in two directions.
2. Vanishing points are the least-squares intersections of each set (smallest singular vector
   of the stacked line equations), so more lines average out clicking error.
3. The two ground vanishing points define the **vanishing line** (horizon) of the ground plane.
4. For an object with base b and top t, the ratio
   `‖b × t‖ / (|l · b| × ‖v × t‖)` (homogeneous coordinates, l the horizon, v the vertical
   vanishing point) is proportional to its height. The target's height is the reference height
   times the ratio of the two objects' ratios.

*Why:* it needs no camera calibration and no survey, only lines that exist in most scenes and
one known height, and its assumptions are explicit (listed in the app and the report).

### 17.2 Distance and speed on a plane

The examiner clicks at least four points on the ground whose positions were surveyed at the
scene. A **homography** from image to ground is fitted by least squares over all points
(OpenCV `findHomography`, method 0) and two points are mapped onto the ground to measure their
distance. Speed is that distance (the same ground-contact point in two frames) divided by the
time between the frames taken from the video's own timestamps, reported in m/s and km/h. The
maximum residual of the survey points is reported as a fit check.

*Why least squares over RANSAC:* with a handful of carefully surveyed points every point should
be used; a mis-surveyed point shows up in the residual and should be fixed, not silently dropped.

### 17.3 Height by camera resection (photogrammetry)

From at least six points surveyed in 3D:

1. The principal point is assumed at the image centre with square pixels; the focal length is
   unknown. For each candidate focal length, the camera pose is solved by **SQPnP** (a globally
   optimal PnP solver) and refined by Levenberg-Marquardt; the cost is the RMS reprojection
   error.
2. The focal length is scanned at 40 log-spaced values from 0.3 to 20 times the image diagonal,
   then refined by 30 golden-section iterations.
3. The ray through the target's base is intersected with the ground (Z = 0); the target's height
   is where the vertical line through that point comes closest to the ray through the clicked top.
4. A reprojection error more than 3 times the clicking uncertainty raises a warning (mis-clicked
   point, mis-surveyed point or lens distortion).

*Why:* it is the computational form of reverse-projection photogrammetry (ASTM E2825), needs no
second image, and recovers the camera's position and focal length as a check on the survey.
*Alternatives:* a full calibration with distortion would need many more control points than a
typical scene survey provides; a scene reconstruction from multiple views is planned.

---

## 18. Recorder (DVR) exports

`forensic/formats.py`.

**Identification.** The first 64 KB are matched against signatures: Dahua DAV (`DHAV`),
Hikvision (`IMKH` header), MPEG program and transport streams, raw H.264 and H.265 elementary
streams, AVI, MP4/MOV, ASF and Matroska. The elementary codec is guessed by counting NAL unit
types (SPS, PPS, IDR for H.264; VPS, SPS, PPS for H.265).

**Remux (stream copy).** Containers FFmpeg can read are copied into MPEG transport stream with
`-c copy` and bit-exact flags: no re-encoding, so every compressed frame is byte-identical to the
recorder's, and the recorder's timestamps are kept. For Hikvision, the 40-byte vendor header is
skipped.

**Carve.** For unknown containers, every Annex B start code is found; each NAL unit is kept if it
is syntactically valid (forbidden bit clear, a type in the valid range) and cut where a known
vendor marker (`DHAV`, `IMKH`, `HXVS` and others) begins, so container bytes are not fed to the
decoder. The result is a raw H.264/H.265 stream; since it has no timestamps, frame n is placed at
n / frame rate seconds and the report says so.

Either way the output is a new evidence item recording the original's hash, the method, the
exact command (with local paths removed) and the FFmpeg version, so a second examiner can repeat
it and get the same bytes.

*Why not transcode:* transcoding (as some players' "export to AVI" does) re-compresses every
frame, destroying detail and the double-encoding evidence; stream copy is lossless. *Why
carving as the fallback rather than vendor SDKs:* SDKs are closed and platform-specific; carving
recovers the video from any container that holds standard H.264/H.265, which covers most
recorders, and is fully described by its rules.

---

## 19. Built-in agents: triage, planner, reviewer

The built-in agents are deterministic programs (rules and measured scores), not language models.
They act only through the same service layer as the examiner, every action is logged with the
agent's name and version, and anything they add to a chain is a *proposal* that stays out of the
findings until an examiner accepts it.

### 19.1 Triage (Smart Report)

`forensic/triage.py`. A fixed battery per media type: images get file metadata, provenance,
A-DJPG and NA-DJPG (the last two only for JPEGs); videos get metadata, provenance, frame
integrity and double encoding (when the codec is supported). A fixed, versioned table maps each
warning code to **red** (a local or structural inconsistency: a region inconsistent with the
compression history, a thumbnail mismatch, an irregular JPEG structure, backwards timestamps, an
AI declaration, invalid credentials, a repeated moving segment) or **yellow** (a processing
history that limits what can be said: re-saving, editing software, missing EXIF, timestamp gaps,
re-encoding). The item takes the highest colour.

*Why fixed rules rather than a learned score:* the full rule table is printed in the report, so
anyone can see why an item was sorted where it was. Green means no check in the battery found an
inconsistency; it is not a finding of authenticity.

### 19.2 Enhancement planner

`forensic/agents/planner.py` and `quality.py`. Given a goal in words, it picks a workflow by
keywords (plate or text; low light; stabilise and integrate; general), builds 3 to 8 candidate
chains of classical filters (with multi-frame super resolution or integration first when a frame
range is given), previews each on the region of interest and ranks them by a no-reference score:

`score = ln(sharpness ratio) + 0.5 × ln(contrast ratio) − 1.5 × max(0, ln(noise ratio)) − 10 × increase in clipped share`

where sharpness is the **Tenengrad** measure (mean squared Sobel gradient), noise is
**Immerkær's** estimate and contrast the standard deviation of luma. The best two become proposed
chains.

*Why these measures:* they are cheap, deterministic and explainable. The score is a ranking aid
only; no-reference quality metrics reward sharpening and contrast that can be inappropriate, so
the examiner judges the result. Learned quality metrics (BRISQUE, NIQE, MUSIQ) were not used
because they are trained on aesthetic judgements of ordinary photos, not on readability of
evidence.

### 19.3 Reviewer

`forensic/agents/reviewer.py`. Does mechanically what a second examiner would: re-hashes every
original, verifies the audit chain, re-runs every rendered chain **twice** from the originals
with no cache (checking both determinism and the recorded hash), re-runs every analysis and
measurement and compares result hashes, verifies the weights of every model used, and flags
generative steps, unjustified ones and unaccepted agent proposals. It changes nothing except
writing its own logged review, which is printed in the report.

---

## 20. How detector accuracy is measured

`forensic/validation/`. `python -m forensic.validation run` generates a corpus of photo-like
synthetic scenes from fixed seeds (sky gradient, multi-scale textured ground, sharp-edged objects,
optional text, sensor noise), applies each manipulation the detectors target (double compression,
crop between saves, splices, resampling, face-like region edits, video re-encoding and frame
deletion with FFmpeg), runs every detector, and writes a **detector card** per detector: task,
threshold, true and false positive rates, AUC, results by condition and known failure modes. The
app shows each card's headline next to the detector and the report prints it. The latest numbers
are in [validation/README.md](validation/README.md).

Thresholds quoted in this document (A-DJPG 0.05, NA-DJPG 10 and 30, CFA 0.15, resampling 9, face z
4, VPF z 4, frame jumps z 8) were calibrated on this corpus; PRNU's PCE 60 comes from the
published large-scale test.

*Why synthetic first:* it gives exact ground truth and isolates each effect, so a regression in
the code is caught by the test suite (which fails if accuracy drops below the published floor).
It is **not** real-world accuracy. Run with `--pristine <folder of camera JPEGs>` to derive the
same manipulations from real camera images, and validate on public sets (Columbia, CASIA, DSO-1,
Dresden, VISION) and on the lab's own material before relying on a detector in casework.

---

## 21. Performance, determinism and the job queue

On this version every algorithm runs on the CPU and, by design, on **one thread**:

- OpenCV is set to one thread with OpenCL off (`FORENSIC_DETERMINISTIC=1`, the default);
- ONNX Runtime runs single-threaded and sequential, one inference at a time;
- deconvolution, PRNU and the FFT notch use NumPy's FFT in 64-bit float.

This guarantees that the same chain gives the same pixels regardless of machine load, which the
"reproduced bit for bit" check and the reviewer depend on. The cost is speed: on a many-core
workstation most cores sit idle, and the slowest operations (non-local means on large frames,
Richardson-Lucy with many iterations, multi-frame super resolution over long ranges, PRNU with
scale search, face checks on long videos) can take seconds to minutes. The GPU is not used.

Work under way in parallel will make the individual algorithms faster, using the workstation's
GPU and cores where results can still be reproduced; this section and the affected algorithm
sections will be updated when it lands.

### 21.1 Several examiners: the job queue (`forensic/jobs.py`)

**What it does.** Every heavy operation started from the interface (render and verify a chain,
each analysis, recorder conversion, Smart Report, triage, camera reference, the review, the
reports) becomes a job in a queue kept in the case database. Jobs run in two lanes:

- the **GPU lane** takes jobs that run a learned model (an AI filter in the chain, an AI
  analysis, or a review or report that will re-run one). It runs **one job at a time** by default
  (`gpu_jobs` in `workbench.ini`, or `FORENSIC_GPU_JOBS`);
- the **CPU lane** takes everything else, **four jobs at a time** by default (`job_workers`, or
  `FORENSIC_JOB_WORKERS`).

When a place in a lane frees up, the next job goes to the examiner with the **fewest jobs
running** in that lane, then to the examiner whose last job **started longest ago**, then to the
oldest request. Each examiner may have at most 50 jobs waiting. Waiting jobs can be cancelled by
their owner or an administrator; running jobs are not interrupted, because a half-finished
render or analysis would leave nothing useful. A job that was running when the workbench stopped
is marked failed on restart, and waiting jobs resume.

**Why two lanes.** The models share one graphics card (an RTX A4000 has 16 GB). Two models
loading at once can exhaust its memory and fail, or thrash, so model jobs are serialised by
default; an administrator can allow two if the models fit twice. Model-free work does not touch
the card, so it should not wait behind a slow model job, and a separate lane lets it continue. (In
this version the models themselves still run on the CPU, as described above; the lane is in
place for when they move to the card, and limiting them to one at a time already bounds their
memory use.)

**Why four CPU jobs.** Each algorithm runs single-threaded for reproducibility (above), so
throughput comes from running several jobs side by side. Four leaves cores and memory for the
interactive previews of everyone using the machine; on a 128 GB workstation six is a reasonable
setting. More jobs than that mostly compete for memory bandwidth on large frames.

**Why examiners take turns.** A plain first-come, first-served queue lets one examiner's batch
(for example triage of a 200-item case) hold up every other examiner's single render. Fair
queuing by running-job count, then by time since last turn, gives each examiner a share of the
machine regardless of how much they queued. It is the round-robin idea used by operating-system
and cluster schedulers, reduced to what a single lab machine needs.

**Why it does not change results.** Scheduling decides only when a job runs. Each job reads the
original, runs single-threaded and records its output hash exactly as before, so a result is
identical however busy the machine was. What a job does is logged under the account that
started it.

**Alternatives considered.**

| Alternative | Why not |
|---|---|
| No queue, run each request at once (the previous version) | Several examiners could overload the machine and its one GPU, and nobody could see what was running. |
| First come, first served | One large batch blocks everyone else. |
| Celery or RQ with Redis | An extra service to install, secure and keep running on an offline machine, for one computer's workload. |
| Strict priorities (for example urgent cases first) | Needs someone to set and police priorities; can starve low-priority work. Can be added later on the same queue table. |

### 21.2 Accounts (`forensic/accounts.py`)

The workbench keeps its own accounts because the lab machine is offline and cannot reach a
directory service. Passwords are stored as **scrypt** hashes (N = 2^14, r = 8, p = 1, about 50 ms
per sign-in) with a random salt; scrypt was chosen over plain salted SHA-256 because it is
deliberately slow and memory-hard, which makes guessing a stolen password file expensive, and
over bcrypt or Argon2 because it is in Python's standard library and adds no dependency. Sessions
last 12 hours in an HttpOnly, SameSite=Strict cookie; five wrong passwords lock a user name for a
minute. The actor recorded in the audit log is taken from the signed-in account, never from
anything the browser sends, so the log's "who" is as trustworthy as the sign-in. Accounts are
disabled, not deleted, so every name in the log stays tied to a known person.

---

## 22. References

Standards and guidelines

- ASTM E2825-21, Standard Guide for Forensic Digital Image Processing.
- SWGDE, Best Practices for Image Authentication, 2018; Best Practices for Video Authentication,
  2018; Technical Overview for Speed Determination from Video, 2019.
- SWGIT, Section 5: Recommendations and Guidelines for Image Processing, 2010.
- ITU-T T.81 (JPEG); ITU-R BT.601-7; JEITA CP-3451 (Exif 2.3); C2PA Technical Specification 2.x;
  IPTC Digital Source Type vocabulary.

Textbooks

- R. C. Gonzalez and R. E. Woods, *Digital Image Processing*, 4th ed., Pearson, 2018.
- R. Hartley and A. Zisserman, *Multiple View Geometry in Computer Vision*, 2nd ed., Cambridge, 2004.
- H. Farid, *Photo Forensics*, MIT Press, 2016.

Papers (in the order they appear)

- F. N. Fritsch and R. E. Carlson, Monotone piecewise cubic interpolation, SIAM J. Numer. Anal. 17(2), 1980.
- K. Zuiderveld, Contrast limited adaptive histogram equalization, Graphics Gems IV, 1994.
- G. Buchsbaum, A spatial processor model for object colour perception, J. Franklin Inst. 310(1), 1980.
- R. Keys, Cubic convolution interpolation for digital image processing, IEEE TASSP 29(6), 1981.
- D. C. Brown, Decentering distortion of lenses, Photogrammetric Engineering 32(3), 1966.
- Z. Zhang, A flexible new technique for camera calibration, IEEE TPAMI 22(11), 2000.
- G. de Haan and E. B. Bellers, Deinterlacing: an overview, Proc. IEEE 86(9), 1998.
- J. W. Tukey, *Exploratory Data Analysis*, Addison-Wesley, 1977.
- C. Tomasi and R. Manduchi, Bilateral filtering for gray and color images, ICCV 1998.
- A. Buades, B. Coll and J.-M. Morel, A non-local algorithm for image denoising, CVPR 2005.
- K. Dabov, A. Foi, V. Katkovnik and K. Egiazarian, Image denoising by sparse 3-D transform-domain collaborative filtering, IEEE TIP 16(8), 2007.
- N. Wiener, *Extrapolation, Interpolation, and Smoothing of Stationary Time Series*, MIT Press, 1949.
- W. H. Richardson, Bayesian-based iterative method of image restoration, JOSA 62(1), 1972.
- L. B. Lucy, An iterative technique for the rectification of observed distributions, Astron. J. 79, 1974.
- N. Dey et al., Richardson-Lucy algorithm with total variation regularization for 3D confocal microscope deconvolution, Microsc. Res. Tech. 69(4), 2006.
- R. Fergus et al., Removing camera shake from a single photograph, SIGGRAPH 2006; A. Levin et al., Understanding and evaluating blind deconvolution algorithms, CVPR 2009; D. Krishnan, T. Tay and R. Fergus, Blind deconvolution using a normalized sparsity measure, CVPR 2011.
- G. D. Evangelidis and E. Z. Psarakis, Parametric image alignment using enhanced correlation coefficient maximization, IEEE TPAMI 30(10), 2008.
- M. Irani and S. Peleg, Improving resolution by image registration, CVGIP: Graphical Models and Image Processing 53(3), 1991.
- S. Farsiu, M. D. Robinson, M. Elad and P. Milanfar, Fast and robust multiframe super resolution, IEEE TIP 13(10), 2004.
- C. Dong, C. C. Loy and X. Tang, Accelerating the super-resolution convolutional neural network, ECCV 2016.
- W. Shi et al., Real-time single image and video super-resolution using an efficient sub-pixel convolutional neural network, CVPR 2016.
- W. Wu, H. Peng and S. Yu, YuNet: a tiny millisecond-level face detector, Machine Intelligence Research 20, 2023.
- N. Krawetz, A picture's worth: digital image analysis and forensics, Black Hat USA, 2007.
- H. Farid, Exposing digital forgeries from JPEG ghosts, IEEE TIFS 4(1), 2009.
- T. Bianchi and A. Piva, Image forgery localization via block-grained analysis of JPEG artifacts, IEEE TIFS 7(3), 2012.
- Z. Lin, J. He, X. Tang and C.-K. Tang, Fast, automatic and fine-grained tampered JPEG image detection via DCT coefficient analysis, Pattern Recognition 42(11), 2009.
- W. Luo, Z. Qu, J. Huang and G. Qiu, A novel method for detecting cropped and recompressed image block, IEEE ICASSP 2007.
- T. Bianchi and A. Piva, Detection of nonaligned double JPEG compression based on integer periodicity maps, IEEE TIFS 7(2), 2012.
- J. Lukáš, J. Fridrich and M. Goljan, Digital camera identification from sensor pattern noise, IEEE TIFS 1(2), 2006.
- M. Goljan, J. Fridrich and T. Filler, Large scale test of sensor fingerprint camera identification, Proc. SPIE 7254, 2009.
- M. Goljan and J. Fridrich, Camera identification from cropped and scaled images, Proc. SPIE 6819, 2008.
- A. C. Gallagher and T. Chen, Image authentication by detecting traces of demosaicing, CVPR Workshops 2008.
- P. Ferrara, T. Bianchi, A. De Rosa and A. Piva, Image forgery localization via fine-grained analysis of CFA artifacts, IEEE TIFS 7(5), 2012.
- M. Kirchner, Fast and reliable resampling detection by spectral analysis of fixed linear predictor residue, ACM MM&Sec 2008.
- A. C. Popescu and H. Farid, Exposing digital forgeries by detecting traces of resampling, IEEE TSP 53(2), 2005.
- U. Ojha, Y. Li and Y. J. Lee, Towards universal fake image detectors that generalize across generative models, CVPR 2023.
- D. Cozzolino et al., Raising the bar of AI-generated image detection with CLIP, CVPR Workshops 2024.
- B. Mahdian and S. Saic, Using noise inconsistencies for blind image forensics, Image and Vision Computing 27(10), 2009.
- J. Immerkær, Fast noise variance estimation, CVIU 64(2), 1996.
- L. Li et al., Face X-ray for more general face forgery detection, CVPR 2020.
- D. Vázquez-Padín et al., Detection of video double encoding with GOP size estimation, IEEE WIFS 2012.
- W. Wang and H. Farid, Exposing digital forgeries in video by detecting double MPEG compression, ACM MM&Sec 2006; Exposing digital forgeries in video by detecting duplication, ACM MM&Sec 2007.
- A. Criminisi, I. Reid and A. Zisserman, Single view metrology, IJCV 40(2), 2000.
- G. Terzakis and M. Lourakis, A consistently fast and globally optimal solution to the perspective-n-point problem (SQPnP), ECCV 2020.
