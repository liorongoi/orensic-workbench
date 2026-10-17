# Deblur benchmarks

Generated 2026-10-15 by `python -m forensic.validation.deblur` (Lucida 0.3.0, NumPy 2.5, x86-64 CPU). Raw numbers,
every setting tried and per-image scores are in `results.json` next to the copy of this page in the project files.

## What was measured

Lucida's deblur filters (Wiener, Richardson-Lucy, total variation) are *non-blind*: the examiner describes the blur and
the filter removes it. They were scored on two public benchmarks with ground truth, using the measures the papers
that use them publish, so the numbers sit beside published ones.

* **Levin et al. 2009** ("Understanding and evaluating blind deconvolution algorithms", CVPR). Four photographs blurred
  by real hand-held camera shake, eight measured kernels, 32 grey images of 255 x 255. Scored as the papers score it:
  PSNR and SSIM after the best shift within ±10 px in quarter-pixel steps and a 15 px border crop (Levin's
  `comp_upto_shift`). Copy used: the one in the SelfDeblur repository (Ren et al., CVPR 2020).
* **IRCNN/DPIR synthetic protocol** (Zhang et al., CVPR 2017 and TPAMI 2021). Sharp test images blurred with Levin's
  eight kernels by circular convolution, plus white noise of 2.55 or 7.65 grey levels from NumPy seed 0; PSNR on
  8-bit images. Set3C (3 colour images) and the seven 256 x 256 images of Set12. "Interior" leaves out a 20 px border:
  the protocol blurs as if the image wrapped around, which Lucida, built for real photographs, does not assume.

Each filter was tried over a grid of its strength setting; the tables give the best setting over the whole set, which
flatters every method equally, and the score at the app's default.

As a reference, **DPIR** (Zhang et al. 2021, a published deep-learning plug-and-play method; DRUNet weights from the
authors' KAIR release) was run here on exactly the same inputs, with the true kernel, following the authors' script.

Not run: Köhler et al. 2012, Lai et al. 2016, Sun et al. 2013, GoPro and RealBlur. Their hosts are blocked from the
build machine. GoPro and RealBlur are also blur that varies across the frame (moving objects), which non-blind uniform
filters do not model.

## Results

### Levin et al. 2009, real camera shake (32 images)

| Method | PSNR (dB) | SSIM | Best setting | PSNR at app default |
|---|---|---|---|---|
| Blurred input | 23.25 | 0.699 | | |
| True kernel, Wiener | 31.26 | 0.901 | nsr 0.01 | 31.26 |
| True kernel, Richardson-Lucy | **32.75** | **0.929** | 150 iterations, tv 0.002 | 30.12 |
| True kernel, Total variation | 31.52 | 0.909 | regularisation 0.001 | 31.36 |
| Closest app blur shape, best filter | 24.38 | 0.748 | | |
| App motion estimate, best filter | 23.98 | 0.706 | | |
| DPIR, true kernel, best noise setting (run here) | 28.98 | 0.846 | | |

### IRCNN/DPIR protocol (true kernel; PSNR / interior PSNR in dB)

| Set, noise | Blurred | Wiener | Richardson-Lucy | Total variation | DPIR (run here) |
|---|---|---|---|---|---|
| Set3C, 2.55 | 16.52 / 16.47 | 22.33 / 23.62 | 23.67 / 26.91 | 23.43 / **27.20** | 34.86 / 34.87 |
| Set12 (7), 2.55 | 19.34 / 19.11 | 24.38 / 25.40 | 25.98 / 28.37 | 26.17 / **28.84** | 33.07 / 32.74 |
| Set3C, 7.65 | 16.32 / 16.28 | 19.92 / 20.65 | 21.52 / 22.52 | 22.47 / **24.57** | 30.31 / 30.29 |
| Set12 (7), 7.65 | 19.01 / 18.77 | 21.05 / 21.49 | 23.52 / 24.26 | 24.75 / **26.13** | 29.22 / 28.80 |

## What it says

1. **With the right kernel, Lucida's classical filters work on real camera shake.** Richardson-Lucy with total
   variation reaches 32.75 dB on Levin's set, up from 23.25 dB blurred. For context, Ren et al. (CVPR 2020, Table 1)
   report about 34.5 dB on this set for Levin's own deconvolution with the true kernel; that figure is quoted from
   memory, as the paper's host is blocked here.
2. **The weak point is describing the blur.** Real camera shake is a curved, uneven path. The closest straight line,
   disk or Gaussian the app can describe gets 24.4 dB, and the app's motion-length estimate 24.0 dB: barely better
   than the blurred input. This is the gap Amped's nonlinear and blind deblurring fill, and the follow-up work adds
   drawn paths, light streaks and blind kernel estimation.
3. **On synthetic blur with noise, a learned method is far ahead.** DPIR beats Lucida's best filter by 4 to 8 dB with
   the same kernel. DPIR scores lower than Lucida on Levin's real photographs (29.0 dB) because it assumes the image
   wraps around at its edges; that number reflects its boundary handling, not its prior. A learned deconvolution
   like DPIR is the biggest single quality gain available, at the cost of a 130 MB model and an AI-assisted label in
   the report.
4. **The defaults leave quality on the table.** Total variation is the best filter on noisy images and its default
   (0.002) is right at low noise; at 7.65 grey levels 0.005 is 2 dB better. Wiener's default noise ratio (0.01) and
   Richardson-Lucy's default (20 iterations, no TV) are 1 to 3 dB below their best settings.

## Re-running

```
python -m forensic.validation.deblur --levin <levin dir> \
    --images set3c=<dir> --images set12=<dir> --kernels <dir of kernel1.npy ... kernel8.npy> \
    --reference DPIR=<outputs dir> --save-inputs <dir> --out <results dir>
```

`--save-inputs` writes the exact degraded inputs, so another method can be run on them; `--reference NAME=DIR`
scores that method's outputs (`<case>.png` or `.npy`) with the same measures. Kernels: Levin09.mat from the KAIR or
DPIR repository, one `.npy` per kernel. The full run takes about 75 minutes on four CPU cores.

## New deblur tools (follow-up)

Four kinds of blur Amped FIVE handles and Lucida did not, now added, each measured against what the app could do before.
Raw numbers: `results-new-filters.json` next to this page in the project files.

| Blur | Before (best existing option) | New tool | Score with the true blur, for reference |
|---|---|---|---|
| Real camera shake, a small light in view (Levin, 32 images) | 24.4 dB, closest straight line/disk/Gaussian | **30.8 dB**, light-streak blur shape (Richardson-Lucy) | 32.8 dB |
| Real camera shake, no hint (Levin, 32 images) | 24.0 dB, app's motion estimate | **30.3 dB**, blind deblur | 32.8 dB |
| Curved shake along random paths (Set12 and Levin photos, 88 images) | 24.8 dB, closest straight line or disk | **28.1 dB**, drawn path (10 clicks); 27.4 dB blind | 32.2 dB |
| Camera roll, blur varies across the frame (11 images) | 27.5 dB, one uniform streak (blurred: 27.4) | **29.1 dB**, variable motion from 4 drawn streaks | |
| Turbulence, 20 shimmering frames (11 scenes) | 26.2 dB, median of frames (one frame: 21.5) | **29.0 dB**, turbulence removal | |

* **Light-streak blur** (`psf: streak` on Wiener, Richardson-Lucy and total variation): a box around the smear a small
  light left is read as the blur kernel. On Levin's set it gets within 2 dB of the true kernel.
* **Drawn-path blur** (`psf: path`): click along the blur's trajectory. With 10 clicks it recovers 3.3 dB over a
  straight line on curved shake; tracing more points along sharp turns gets closer to the true-kernel score.
* **Blind deblur** (`deblur.blind`): estimates the kernel from the image's strong edges (L0 sparse-gradient prior,
  coarse to fine), then deconvolves. On Levin's real shake it recovers 7 dB with no input from the examiner and fails
  (stays near the blurred score) on 2 of the 32 images. Its default edge selection (0.01) was chosen on this same set.
* **Variable motion** (`deblur.variable`): draw two or more short streaks across the frame; the blur is interpolated
  between them and each tile is deblurred with its own. A tile-wise *blind* version was tried first and made images
  worse (tiles hold too few edges to estimate from), so it was dropped.
* **Turbulence removal** (`stack.turbulence`): registers every frame onto the per-pixel median with dense optical
  flow, fuses, then removes the residual softness. Needs a fixed camera and a static scene.

The curved-shake, camera-roll and turbulence tests are synthetic (generated from the sharp images with fixed seeds),
because the public sets with such blur (Köhler, Lai, GoPro, RealBlur) could not be downloaded here; the drawn-path and
streak inputs emulate an examiner's clicks with up to half a pixel of error.
