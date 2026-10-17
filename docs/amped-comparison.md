# Amped FIVE and Authenticate compared with Lucida

Every enhancement filter, authentication analysis and video tool in Amped's two products, set beside the Lucida equivalent and the method each one uses. Lucida's side comes from the [algorithm guide](algorithms.md) and the code; the § numbers point into it. First compared 15 October 2026, updated 17 October 2026.

**Verdicts:** Equivalent, Lucida broader, Lucida narrower, Amped only, Lucida only.

**Where the Amped entry comes from:** *Amped docs* means Amped's product pages, blog, update notes or videos say it. *Method inferred* means Amped names the tool but the algorithm is our reading of the literature. *Inferred* means it is not confirmed in Amped's published material. Amped rarely publishes filter internals, so treat inferred entries as informed reading, not as Amped's statement.

## Coverage by area

| Product area | Rows | Equivalent | Lucida broader | Lucida narrower | Amped only | Lucida only |
|---|---|---|---|---|---|---|
| Amped FIVE | 38 | 21 | 2 | 10 | 3 | 2 |
| Amped Authenticate | 27 | 15 | 1 | 6 | 3 | 2 |
| Video and DVR | 10 | 3 | 0 | 5 | 2 | 0 |
| Workflow and integrity | 6 | 3 | 2 | 0 | 0 | 1 |

## What stands out

### Where Amped is still ahead

- Breadth of reference data: 14,000+ quantisation tables against Lucida's 282 camera models, and far more DVR formats, including encrypted ones.
- Face-level GAN detection with a box per face. Lucida's generated-image detector scores the whole image.
- Social media identification, Huffman-table and hex viewers, PRNU on video, HEVC coding-unit views and motion detection.
- Presentation tools: annotation with tracking, timeline, multiview, video writers, audio redaction.

### Closed since 15 Oct

- Merged: camera QT lookup, JPEG dimples, LGA, PRNU tampering map, fusion map, shadows / reflections / perspective, a bundled GAN and diffusion detector, macroblock / QP / motion-vector views, and more recorder sources (AVI FourCCs, private TS types, RTSP captures).
- Deblurring: drawn-path and light-streak blur shapes, blind, variable-motion and turbulence deblurring.

### Where Lucida is ahead or different

- Every method is named and cited in the app and report, with measured detection and false-alarm rates (detector cards). Amped publishes neither for most filters.
- Total-variation deconvolution (FTVd), Richardson-Lucy and a cepstrum blur estimate, all with stated solvers.
- Motion-compensated video denoise, sigma-clipped integration and model-based multi-frame super resolution (back-projection).
- CFA demosaicing traces, offline C2PA validation and classical face-consistency checks. The fusion map is a fixed Dempster-Shafer rule that lists every contribution, where Amped labels its own as AI-based.
- Geometric checks report the tolerance up to which a conflict holds, not just feasible or unfeasible at one setting.
- Bit-exact re-runs, a hash-chained audit log and an independent reviewer, plus walled-off generative enlargement (Real-ESRGAN, FSRCNN, ESPCN) that FIVE does not offer.

### Deblur benchmark

On Levin et al. 2009's real camera shake, Lucida's Richardson-Lucy with TV reaches 32.8 dB when given the true blur, up from 23.3 dB. The blind filter reaches 30.3 dB on its own, and a light streak in view gives 30.8 dB. On the synthetic DPIR protocol with noise, the learned DPIR method is 4 to 8 dB ahead of Lucida's best filter. There is no published benchmark of Amped's deblurring to compare against. Full tables are in [the deblur benchmarks](validation/deblur-benchmarks.md).

## Amped FIVE

### Exposure and colour

*FIVE Adjust, Channels and Extract filters*

| Capability | Amped tool and method | Lucida equivalent and method | Verdict | Gap or note |
|---|---|---|---|---|
| Levels, brightness, contrast, exposure | **Levels; Contrast/Brightness; Exposure** (Method inferred)<br>Input/output black and white points with gamma; linear contrast about a centre. | **Levels; Brightness and contrast** §5.1–5.2<br>Linear mapping with gamma; contrast scaled about mid-grey, in 32-bit float. | Equivalent | No separate exposure (stops) control in Lucida; levels covers it. |
| Curves | **Curves** (Method inferred)<br>Control-point tone curve; interpolation not published. | **Curves** §5.3<br>PCHIP monotone cubic (Fritsch-Carlson) through a 4096-entry table, so tones never swap order. | Equivalent | Lucida states why the curve cannot reverse tones; Amped does not say. |
| Histogram equalisation and CLAHE | **Histogram Equalization; CLAHE; Contrast Stretch** (Method inferred)<br>Standard global and tile-based equalisation. | **Histogram equalisation; Local contrast (CLAHE)** §5.4–5.5<br>Zuiderveld CLAHE on L* of CIE L*a*b* only, so hues do not shift. | Equivalent | Contrast Stretch (percentile auto-stretch) has no direct Lucida filter. |
| Automatic adjustment | **Smart Adjust; ACE (automatic colour equalisation), with a faster Global mode** (Method inferred)<br>ACE likely follows Rizzi, Gatta and Marini (2003); Smart Adjust's method is not published. | **No one-click auto adjust** §19.2<br>The enhancement planner builds and ranks candidate chains by a stated no-reference score instead. | Lucida narrower | No ACE or Smart Adjust. CLAHE and low-light cover most of the same footage. |
| Illumination correction | **Homomorphic filter** (Method inferred)<br>Log of the image, high-emphasis filter in the frequency domain to separate illumination from reflectance (textbook method). | **Low-light enhancement (LIME)** §5.9<br>Illumination map from the max channel, bilateral-smoothed, gain capped at 6×, NLM blended into lifted shadows (Guo, Li and Ling 2017). | Equivalent | Same reflectance × illumination model, different solution. No homomorphic filter in Lucida. |
| White balance | **White Balance, optionally on a selected neutral area** (Amped docs)<br>Gains from a selection or automatic. | **Colour balance** §5.6<br>Grey world (Buchsbaum 1980) or manual per-channel gains. | Lucida narrower | Lucida has no 'click a neutral area' mode; gains must be typed. |
| Studio to full range | **Colour range fix in the loader** (Amped docs)<br>Limited (16–235) to full range. | **Video range to full range** §5.8<br>Fixed BT.601/709 mapping on Y, optional chroma scaling by 255/224. | Equivalent | — |
| Channels and colour spaces | **Grayscale, Extract/Replace Channel, Channel Mixer, Color Conversion, HSV, RGB↔BGR, Negative, Threshold** (Amped docs)<br>Per-channel arithmetic and colour-space conversions. | **Greyscale or channel; Invert; Channel gains** §5.7<br>Rec. 601 luma, single channel, 1 − v. | Lucida narrower | No channel mixer, HSV or other colour spaces, no threshold. |
| Component separation and colour deconvolution | **Component Separation; Color Deconvolution** (Method inferred)<br>Likely principal components and Ruifrok-Johnston stain separation; Amped does not say. | **None** | Amped only | Used on latent prints and patterned backgrounds. |

### Geometry and interlace

*FIVE Edit and Interlace filters*

| Capability | Amped tool and method | Lucida equivalent and method | Verdict | Gap or note |
|---|---|---|---|---|
| Crop, flip, rotate | **Crop, Flip, Rotate (90/180 buttons or free angle)** (Amped docs)<br>Exact moves for right angles, interpolation otherwise. | **Crop, flip, rotate** §6.2–6.3<br>Exact array moves for 90/180/270; other angles resampled with an enlarged canvas. | Equivalent | — |
| Resize and interpolation | **Resize (interpolation choice); Smart Resize; Resize 1:1 (DPI from a scale)** (Method inferred)<br>Smart Resize is an edge-aware upscaler whose method is not published. | **Resize; interpolation choice on every resampling step** §6.1, §6.4<br>Nearest, bilinear, bicubic (Keys, a = −0.75), Lanczos 8×8. | Lucida narrower | No edge-directed upscaler and no 1:1 print scaling. |
| Aspect ratio | **Aspect Ratio** (Amped docs)<br>Rescale to display ratio for non-square pixels. | **Aspect-ratio correction** §6.4<br>Same. | Equivalent | — |
| Perspective | **Correct Perspective** (Amped docs)<br>Four-point rectification. | **Perspective correction** §6.5<br>Planar homography from four clicked corners (Hartley and Zisserman). | Equivalent | — |
| Lens distortion | **Undistort (trace lines that should be straight); Correct Fisheye; Unroll (360°); Camera Calibration from a checkerboard (2025)** (Amped docs)<br>Plumb-line estimation from traced lines; fisheye and 360 projections. | **Lens distortion** §6.6<br>Brown-Conrady k1–k3, p1, p2 typed in; no automatic estimate. | Lucida narrower | No line-tracing estimate, fisheye model, 360 unroll or checkerboard calibration. |
| Interlacing | **Line Doubling, Interleave, Deinterlace, Field Shift** (Amped docs)<br>Field separation and recombination. | **Deinterlace** §6.7<br>Field (line averaging), blend, split; motion-adaptive methods rejected on purpose. | Equivalent | No field-shift (swap field order) filter. |

### Noise reduction and sharpening

*FIVE Denoise, Sharpen and Fourier*

| Capability | Amped tool and method | Lucida equivalent and method | Verdict | Gap or note |
|---|---|---|---|---|
| Spatial denoise | **Averaging, Gaussian, Median, Bilateral, Nonlocal Means filters** (Inferred)<br>Standard filters by name. | **Median, Gaussian, Bilateral, Non-local means** §7.1–7.4<br>Tukey median; Tomasi-Manduchi bilateral; Buades-Coll-Morel NLM in L*a*b*. | Equivalent | Amped's exact denoise list is from filter names, not confirmed here. |
| Deblocking and Wiener denoise | **Deblocking; Wiener filter** (Inferred)<br>Compression-block smoothing and adaptive Wiener denoise. | **None** §7.2<br>Gaussian blur is suggested for blocking before other steps. | Amped only | — |
| Periodic noise | **Fourier: automatic peak selection plus manual boxes (U key); output as spectrum, image or removed complement** (Amped docs)<br>Frequency-domain notching. | **Periodic noise removal (FFT notch)** §7.5<br>Peaks found by a stated threshold over a median-filtered log spectrum, Gaussian notches, protected centre. | Lucida narrower | No visual spectrum editor or 'show what was removed' output. Lucida's automatic peaks are repeatable from parameters. |
| Sharpening | **Unsharp Masking; Laplacian Sharpening** (Inferred)<br>Standard high-pass boosting. | **Unsharp mask** §7.6<br>Amount × (image − Gaussian), optional threshold. | Lucida narrower | No Laplacian sharpen. |

### Deblurring

*FIVE Deblurring filters*

| Capability | Amped tool and method | Lucida equivalent and method | Verdict | Gap or note |
|---|---|---|---|---|
| Defocus blur | **Optical Deblurring** (Method inferred)<br>Deconvolution with a disk PSF; solver not published. | **Wiener, Richardson-Lucy or TV deconvolution with a disk PSF** §8.1–8.4<br>Anti-aliased disk, symmetric padding, 64-bit FFT. | Equivalent | Lucida offers three solvers; Amped's solver is unknown. |
| Linear motion blur | **Motion Deblurring, including a 'replicas' mode for ghosted double images** (Method inferred)<br>Deconvolution with a line PSF; solver not published. | **Motion PSF with Wiener, Richardson-Lucy (+TV) or TV (FTVd); Estimate motion blur** §8.1–8.5<br>Line PSF sampled 16×/px; cepstrum search over 0–179° and 3–60 px suggests length and angle. | Equivalent | No replicas (multiple-copy) PSF. Lucida adds an automatic blur estimate. |
| Nonlinear motion | **Nonlinear Deblurring: draw the blur path with the mouse** (Amped docs)<br>User-drawn PSF. | **Drawn-path and light-streak blur shapes** §8.1<br>Path: clicked polyline sampled at 1/16 px. Streak: the smear of a small light read from a box as the measured kernel. Both feed the Wiener, RL and TV solvers. | Equivalent | Curved shake: 28.1 dB with 10 clicks against 24.8 dB for the closest straight line. Light streak on Levin: 30.8 dB against 32.8 dB with the true kernel. |
| Spatially varying motion | **Variable Motion Deblurring** (Amped docs)<br>Blur that changes across the frame or between frames, tuned frame by frame. | **Variable motion** §8.7<br>Two or more drawn streaks interpolated per tile, each tile deconvolved by TV, blended with sin² windows (Hirsch et al. 2010). | Equivalent | Camera roll: 29.1 dB against 27.5 dB for one uniform streak. |
| Blind deconvolution | **Blind Deconvolution** (Amped docs)<br>Estimates PSF and image together; method not published. | **Blind deconvolution** §8.6<br>Deterministic L0 sparse-gradient kernel estimate, coarse to fine (Xu, Zheng and Jia 2013; Pan et al. 2014), then TV deconvolution; no random start. | Equivalent | Levin real shake: 30.3 dB with no input against 32.8 dB with the true kernel; fails on 2 of 32 images. |
| Atmospheric turbulence | **Turbulence Deblurring** (Amped docs)<br>Multi-frame correction of heat shimmer. | **Turbulence removal** §8.8<br>Frames warped onto the per-pixel median with Farnebäck flow, fused, repeated once, then TV deconvolution (Zhu and Milanfar 2013 outline). | Equivalent | 29.0 dB against 26.2 dB for a plain median of 20 frames. Needs a fixed camera and a static scene. |
| TV-regularised deconvolution | **Not named** (Inferred) | **Total-variation deconvolution (FTVd)** §8.4<br>Half-quadratic splitting, exact FFT sub-problems, β continuation 1→256 (Wang, Yang, Yin and Zhang 2008). | Lucida only | Best filter on noisy images in the DPIR protocol, for example 28.8 dB (interior) on Set12 at low noise. |

### Multi-frame methods

*FIVE Select Frames, Stabilization and Integrate*

| Capability | Amped tool and method | Lucida equivalent and method | Verdict | Gap or note |
|---|---|---|---|---|
| Frame selection | **Range Selector, Sparse Selector, Remove Duplicates (incl. MD5 mode), I-frames, Auto Selector, Demultiplexer, Remove Frames** (Amped docs) | **Frame range on the chain source (up to 600), Pick frame** §4, §9.3<br>Frames chosen by decoded index. | Lucida narrower | No sparse or I-frame selection, no demultiplexer for multiplexed CCTV. |
| Stabilisation and registration | **Stabilization; Local Stabilization; Perspective Stabilization; hybrid tracking** (Method inferred)<br>Tracking method not published. | **Stabilise / register frames (ECC)** §9.1<br>Enhanced correlation coefficient (Evangelidis and Psarakis 2008), optional ROI, translation to homography; non-converged frames left and reported. | Equivalent | ROI plus homography covers local and perspective modes. |
| Frame integration | **Frame Averaging** (Amped docs)<br>Plain per-pixel mean (Amped's blog). | **Frame integration** §9.2<br>Mean, median, or sigma-clipped mean. | Lucida broader | Lucida adds robust modes that reject passing objects. |
| Multi-frame super resolution | **Super Resolution; Perspective Super Resolution** (Amped docs)<br>Pools pixels from aligned frames onto a finer grid, with a sharpening setting; maths not published. | **Multi-frame super resolution** §9.4<br>ECC registration, shift-and-add start, iterative back-projection (Irani and Peleg 1991) with median combining (Farsiu 2004). | Equivalent | Lucida's is model-based and states its PSF; Amped's sharpening step is opaque. |
| Temporal denoise | **Temporal Smoothing; Motion Smoothing** (Method inferred)<br>Neighbouring-frame averaging; motion handling not published. | **Video denoise (motion-compensated)** §9.5<br>DIS optical flow, per-pixel weights from colour difference, then light NLM (after Liu and Freeman 2010). | Lucida broader | Lucida follows motion explicitly so moving objects do not trail. |
| Two-image alignment and differencing | **Perspective Registration / Perspective Aligner (point pairs); Video Mixer absolute difference with similarity metrics** (Amped docs) | **Compare modes: swipe, side by side, flip** Viewer<br>Visual comparison of original and result only. | Lucida narrower | No aligner between two evidence items, no difference blend or similarity metric. |

### AI, redaction and presentation

*FIVE Presentation and redaction*

| Capability | Amped tool and method | Lucida equivalent and method | Verdict | Gap or note |
|---|---|---|---|---|
| Generative enlargement | **Not offered in FIVE** (Inferred)<br>Amped's public position is that generative AI enhancement is not suitable for evidence. | **AI super resolution (FSRCNN, ESPCN); AI enhance and enlarge (Real-ESRGAN)** §10.1–10.2<br>Walled off: 'ILLUSTRATIVE ONLY' watermark, no measurement, logged 20-character justification, reviewer flag. | Lucida only | Lucida keeps multi-frame super resolution as the evidential method. |
| Redaction | **Annotate hide/blur/pixelate with manual, assisted or keyframe tracking; Audio Redaction (silence or tone)** (Amped docs)<br>Template tracking box by box. | **Face redaction** §10.3<br>YuNet face detection (Wu, Peng and Yu 2023) plus manual regions; pixelate, blur or blank. | Lucida narrower | Lucida finds faces automatically but has no tracking of arbitrary objects and no audio redaction. |
| Annotation and court output | **Annotate (arrows, text, spotlight, magnifier, tracking), Add Text/Timestamp, Timeline, Multiview, Picture-in-picture, Video/Sequence/Image writers, FFV1 lossless, writing queue** (Amped docs) | **Lossless PNG export per chain; converted recorder exports** §2.2, §18 | Amped only | Presentation rather than analysis, but examiners rely on it for court. |

### Measurement

*FIVE Measure*

| Capability | Amped tool and method | Lucida equivalent and method | Verdict | Gap or note |
|---|---|---|---|---|
| Height from one image | **Measure 3D: two X, Y and Z lines, a reference length, result ± error** (Method inferred)<br>Likely Criminisi single-view metrology; error method not published. | **Height by single-view metrology** §17.1<br>Criminisi, Reid and Zisserman 2000, least-squares vanishing points, 95% interval by seeded Monte Carlo (2000 runs). | Equivalent | Lucida states how its interval is computed. |
| Distance and speed on a plane | **Measure 1D/2D; Speed Estimation 2D** (Amped docs)<br>Plane rectification. | **Distance and speed on a plane** §17.2<br>Least-squares homography from ≥4 surveyed points, speed from the video's own timestamps, residual check. | Equivalent | — |
| Camera model | **Camera Calibration from checkerboard frames (2025)** (Amped docs)<br>Intrinsics from a pattern filmed by the same camera. | **Height by camera resection** §17.3<br>SQPnP pose with a focal-length scan from ≥6 surveyed scene points; reprojection check. | Equivalent | Different inputs: Amped needs the camera to film a checkerboard; Lucida needs a scene survey. |

## Amped Authenticate

### File and metadata

*Authenticate Overview and File analysis*

| Capability | Amped tool and method | Lucida equivalent and method | Verdict | Gap or note |
|---|---|---|---|---|
| File format warnings | **File Format: red and orange warnings; editor blacklist of 100+, date mismatch, no thumbnail, no chroma subsampling, QT mismatch, optimised Huffman tables; rules editable per lab** (Amped docs) | **File metadata consistency checks** §11.1<br>Editor names, modify ≠ capture time, EXIF size mismatch, XMP history, thumbnail aspect > 2%, irregular structure, trailing bytes. | Lucida narrower | Lucida's rules are fixed; no Huffman or chroma-subsampling warning. |
| EXIF, GPS, JPEG structure, thumbnail | **EXIF (70+ fields, map button), JPEG Structure, Thumbnail and preview viewer** (Amped docs) | **EXIF, GPS, XMP; JPEG marker walk; thumbnail check** §11.1<br>Bounds-checked marker walk as untrusted input; thumbnail decoded and compared. | Equivalent | — |
| Quantisation tables | **JPEG QT: match against 14,000+ tables, lists compatible camera models** (Amped docs) | **Quantisation tables vs camera database** §11.1<br>Exact lookup in an offline database: 282 camera models (405 table sets) from CC0 and freely licensed collections, libjpeg 1–100, plus a lab layer that grows from the lab's own reference images. | Lucida narrower | Much smaller database. Measured: 98% of re-saves caught, but 25% false mismatch on held-out originals, so the finding is yellow. |
| Huffman tables, hex, social media | **JPEG HT (standard vs optimised); Hex viewer and strings; Social Media Identification (signatures for about 10 platforms plus file-name patterns)** (Amped docs) | **None** | Amped only | — |
| Content credentials | **Adobe raw-conversion watermark filter (2026); C2PA support not found in Amped's material** (Inferred) | **Content credentials and AI provenance** §14.1<br>Offline C2PA validation with the reference library, IPTC digital source type, generator names in metadata. | Lucida only | — |

### Global analysis

*Authenticate Global analysis*

| Capability | Amped tool and method | Lucida equivalent and method | Verdict | Gap or note |
|---|---|---|---|---|
| JPEG Ghost (global) | **JPEG Ghost plot: 50 recompressions, minima mark earlier qualities** (Method inferred)<br>Farid (2009) family. | **JPEG Ghost** §12.2<br>Re-saves at 50–95 in steps of 5, block-averaged difference maps, global curve minima (Farid 2009). | Equivalent | — |
| Double compression (global) | **DCT plot: comb-shaped coefficient histogram** (Amped docs)<br>Visual reading of quantisation traces. | **Aligned double JPEG global test** §12.3<br>Mixture fit per AC frequency, score, detection at 0.05, first-quality estimate (Bianchi and Piva 2012). | Lucida broader | Lucida turns the same histogram into a statistical test with a validated threshold. |
| Resampling | **Correlation plot** (Method inferred)<br>Periodic correlation from scaling or rotation; method not published (Popescu-Farid type likely). | **Resampling trace** §14.2<br>Kirchner (2008) fixed-predictor p-map spectrum, peaks at 9× background, JPEG grid frequencies ignored. | Equivalent | — |
| Camera JPEG dimples | **JPEG Dimples: grid strength vs threshold 13.0** (Amped docs)<br>Agarwal and Farid (WIFS 2017), as Amped cites. | **JPEG dimples** §12.7<br>Residual averaged over the 8×8 grid, row and column model removed by median polish, robust z ≥ 13 (Agarwal and Farid 2017), plus a local missing-dimple map. | Equivalent | Only 11% of 56 real camera originals had a dimple, and 9% still did after re-saving, so absence means nothing. |
| Screen recapture | **Fourier view for LCD recapture** (Amped docs)<br>Regular moiré frequencies. | **None as an analysis** §7.5<br>FFT notch exists only as an enhancement filter. | Amped only | — |
| Demosaicing traces | **Not listed by Amped** (Inferred) | **CFA traces** §14.2<br>Green-lattice prediction error log ratio in 32-px blocks (Gallagher and Chen 2008; Ferrara et al. 2012), block map. | Lucida only | — |

### Local analysis (tamper maps)

*Authenticate Local analysis*

| Capability | Amped tool and method | Lucida equivalent and method | Verdict | Gap or note |
|---|---|---|---|---|
| Error level analysis | **ELA at several qualities** (Method inferred)<br>Krawetz (2007). | **ELA** §12.1<br>Re-save at q90 4:4:4, ×20 difference, share of outlier 8×8 blocks; never used by triage. | Equivalent | — |
| Aligned double JPEG map | **ADJPEG** (Method inferred)<br>From the EU MAVEN project; likely Bianchi and Piva (2012). | **A-DJPG localisation** §12.3<br>Per-block likelihood ratio, 3×3 smoothing, warning at 1–40% of blocks above 0.8. | Equivalent | — |
| Non-aligned double JPEG map | **NADJPEG** (Method inferred)<br>MAVEN project; likely Bianchi and Piva (2012). | **NA-DJPG grid** §12.4<br>Energy dip over all 64 offsets, median-polish row/column model, robust z thresholds 10 and 30, sliding-window map. | Equivalent | — |
| JPEG Ghost map | **Ghost map with Kolmogorov-Smirnov test; turns all-green when conditions are unfavourable** (Amped docs) | **JPEG Ghost montage** §12.2<br>One map per re-save quality. | Lucida narrower | No statistical test per region and no self-censoring. |
| Blocking and DCT maps | **Blocking Artifacts; DCT Map; JPEG Dimples Map** (Method inferred)<br>Blocking likely a block-artefact-grid method (Li et al. 2009). | **JPEG dimples map; NA-DJPG grid map** §12.4, §12.7<br>Missing-dimple areas found 45% of the time at 10% false alarms. | Lucida narrower | No block-artefact-grid map or DCT map. |
| Fusion map | **Fusion Map: combines ADJPEG, NADJPEG and Ghost; flagged as AI-based since 2026** (Amped docs) | **Fusion map** §12.10<br>Dempster-Shafer fusion of A-DJPG, NA-DJPG, dimples and noise with fixed reliabilities (Fontani et al. 2013); each outlined area lists the analyses that support it. | Equivalent | Measured: 78–88% of pasted areas found; false outlines on 0–5% of untouched images. |
| Noise map | **Noise Map (DCT block settings 4-4, 4-8, 4-16)** (Amped docs) | **Noise level map** §12.6<br>PCA noise estimate (Pyatykh et al. 2013) per window, compared with the image's own noise-level function by brightness, unreliable windows hatched. | Equivalent | Lucida reports measured false-outline rates and keeps it out of triage. |
| Luminance gradient | **LGA** (Inferred)<br>Luminance gradient analysis; method not published. | **Luminance gradient (LGA)** §12.8<br>Sobel gradient after 1.5 px Gaussian, direction as hue and strength as brightness (Krawetz 2007; Johnson and Farid 2005). | Equivalent | A visual aid with no automatic decision, so no accuracy card. |
| Copy-move | **Clones Blocks; Clones Keypoints (lines join matches)** (Method inferred)<br>Block and keypoint families. | **Clone (copy-move) detection** §12.5<br>SIFT matching with g2NN and RANSAC affine (Amerini 2011) plus DCT block matching (Fridrich 2003), both verified densely by local correlation. | Equivalent | Lucida publishes TPR 0.7 / 0.5 and zero false positives on six real photos. |
| Local resampling and saturation | **Correlation Map; Saturation Map** (Amped docs) | **Resampling is global only; noise map hatches near-black and saturated windows** §12.6, §14.2 | Lucida narrower | — |
| PRNU tampering (still) | **PRNU Map; PRNU Tampering: block-wise PCE** (Amped docs) | **PRNU tampering map** §12.9<br>Per-window correlation compared with a content-based prediction, refitted without outliers; only shown when the whole image matches (Chen, Fridrich, Goljan and Lukáš 2008). | Equivalent | Measured: 65% of areas pasted from another camera and 83% of copied areas found, no false outlines. |

### Camera identification

*Authenticate Camera identification*

| Capability | Amped tool and method | Lucida equivalent and method | Verdict | Gap or note |
|---|---|---|---|---|
| PRNU source identification | **PRNU: CRP from a folder (25–50 images), PCE vs threshold (45, later 60), rotation and scale search, batch table, CRP-vs-CRP comparison** (Amped docs)<br>PCE as Amped states; denoiser not published. | **Camera identification (PRNU)** §13<br>Mihçak wavelet denoiser, zero-mean, DFT Wiener, ML fingerprint, PCE 60 (Goljan et al. 2009), 4 rotations, crop search, 100–50% scales, hypotheses counted. | Equivalent | No CRP-vs-CRP comparison or folder batch table in Lucida. |

### Deepfake and synthetic media

*Authenticate Deepfake detection*

| Capability | Amped tool and method | Lucida equivalent and method | Verdict | Gap or note |
|---|---|---|---|---|
| Face swaps and GAN faces | **Face GAN Deepfake: trained detector, GAN / Not GAN with 0–1 score, 'uncertain' below 0.8, collage for the report** (Amped docs)<br>Network and training data not published. | **Face consistency; generated-image detector** §14.3, §15<br>Classical noise and detail z-scores per face against 200 places in the same image; the learned detector (below) catches GAN images but scores the whole image. | Lucida narrower | No per-face trained classifier, so a GAN face pasted into a real photo is usually missed. |
| Fully generated images | **Diffusion Model Deepfake: per-generator compatibility (Flux, SD, DALL-E, Midjourney…), GPU, batch, model version per row** (Amped docs)<br>Network not published. | **Generated-image detector (Community Forensics)** §14.3–14.4<br>ViT-S/16 trained on about 4,800 generators (Park and Owens, CVPR 2025), MIT weights, authors' protocol, uncertain band 0.2–0.8; a lab can install another model in the slot. | Equivalent | Measured: 84% caught at 0.4% false alarms (AUC 0.98); 64% after JPEG 75; only 35–65% on Firefly, Midjourney 6.1 and Ideogram 2. No per-generator scores. |

### Geometric analysis

*Authenticate Geometrical analysis*

| Capability | Amped tool and method | Lucida equivalent and method | Verdict | Gap or note |
|---|---|---|---|---|
| Shadows, reflections, perspective | **Shadows; Reflections; Perspective: wedge constraints, live feasible / unfeasible, minimal unfeasible set** (Method inferred)<br>Likely Kee, O'Brien and Farid (2013) and O'Brien and Farid (2012). | **Geometric consistency** §12.11<br>Same published methods: clicked pairs become wedges, exact feasibility test, smallest conflicting set by exhaustive search, and the tolerance up to which a conflict persists. | Equivalent | Measured: no false conflicts at 1–2 px click error; a constraint turned by 20° found in 84–88% of sets, under 15% at 5°. |
| Sun position and location | **Map and SunCalc links from EXIF GPS** (Amped docs)<br>Online lookups. | **None** §1<br>Lucida is offline by design. | Amped only | — |

## Video and DVR

### DVR exports and decoding

*FIVE Convert DVR, loaders, Advanced File Info*

| Capability | Amped tool and method | Lucida equivalent and method | Verdict | Gap or note |
|---|---|---|---|---|
| Proprietary recorder formats | **Convert DVR: copy stream if possible, else transcode; .clean, .time, .mkv and .log outputs; fallback procedures; encrypted formats (2026)** (Amped docs)<br>Large, continuously updated vendor list. | **Recorder (DVR) exports** §18<br>Native parsers for Dahua DAV, Hikvision IMKH / MPEG-PS, HiSilicon, Xiongmai, transport streams with private stream types, AVI with vendor FourCCs and RTSP captures (pcap, pcapng); stream-copy remux; carving fallback; advice for closed formats. | Lucida narrower | Fewer vendors and no encrypted formats. Lucida never transcodes and reports clock jumps, lost packets and dropped frames. |
| Frame-accurate decoding | **FFMS frame-accurate decoder, DirectShow fallback** (Amped docs) | **FFmpeg by decoded frame index** §3.2<br>select=between(n,…), cached frames byte-identical to a fresh decode. | Equivalent | — |
| File and frame information | **Advanced File Info: MediaInfo, FFprobe, ExifTool, frame and GOP analysis, per-frame MD5, hex, RIFF and Atom viewers** (Amped docs) | **Video metadata** §11.2<br>ffprobe container and stream data, I/P/B sequence, GOP lengths, timestamp gap and order warnings. | Lucida narrower | No hex, RIFF or MP4 atom viewer. |
| Timestamps | **Load Timestamp, Adjust Timestamp, PTS playback, .time extraction** (Amped docs) | **Timing summary on conversion** §18<br>Recorder clock span, gaps over 1.5 intervals, backwards timestamps listed. | Equivalent | — |

### Video authentication

*FIVE Verify and Authenticate Video mode*

| Capability | Amped tool and method | Lucida equivalent and method | Verdict | Gap or note |
|---|---|---|---|---|
| Double encoding | **VPF: intra/predicted/skip plot, periodicity vs 0.60, estimated earlier GOP** (Method inferred)<br>Variation of prediction footprint (Vázquez-Padín et al.), by name. | **Double encoding (VPF)** §16.1<br>Macroblock counts from FFmpeg, robust z ≥ 4 on P-frames, ≥3 regularly spaced candidates (Vázquez-Padín 2012). | Lucida narrower | Lucida does not support HEVC; Amped has HEVC coding-unit tools. |
| Bitstream viewers | **Macroblocks (type overlay, QP heat map), Motion Vectors, Coding Tree Units (HEVC)** (Amped docs) | **Macroblock, QP and motion-vector view** §3.3<br>Taken from FFmpeg's own decoder (-debug mb_type+qp, codecview), fixed QP scale per codec, forward and backward vectors in two colours. | Lucida narrower | H.264, MPEG-1/2/4 and H.263 only; no HEVC coding units. |
| Continuity | **Block Difference plot; Channels plot (average RGB per frame); frame hash duplicates** (Amped docs) | **Frame duplication and deletion** §16.2<br>Thumbnail differences, repeated frames, robust-z jumps, repeated moving segments (Wang and Farid 2007), timestamp gaps. | Equivalent | — |
| PRNU on video | **CRP from video (detects stabilisation); source ID; PRNU tampering per frame group** (Amped docs) | **None** | Amped only | — |
| Generated or swapped faces in video | **Diffusion Model Deepfake for video (June 2026): per-frame scores and threshold plot** (Amped docs) | **Face consistency over sampled frames** §15<br>Flags frame-to-frame fluctuation of the face noise ratio (σ > 0.35 over ≥5 frames). | Lucida narrower | — |
| Motion detection | **Motion Detection: motion plot, ROI, live threshold** (Amped docs) | **None** | Amped only | — |

## Workflow and integrity

### Triage, reporting and reproducibility

*Both products*

| Capability | Amped tool and method | Lucida equivalent and method | Verdict | Gap or note |
|---|---|---|---|---|
| Triage | **Smart Report: property check first, greens skipped, automatically chosen local filters, deepfake results; HTML with traffic lights; CLI** (Amped docs) | **Smart Report (triage)** §19.1<br>Fixed battery per media type, versioned red/yellow rule table printed in the report; JPEGs now also get the QT lookup and dimples. | Equivalent | Lucida runs every check on every item (rules 1.3.0). |
| Detector accuracy | **Not published per filter** (Inferred) | **Detector cards** §20<br>Synthetic corpus from fixed seeds plus real-photo runs; TPR, FPR, AUC and failure modes per detector, shown in app and report. | Lucida only | — |
| Reproducibility and audit | **Project re-checks input MD5; report lists parameters, descriptions and references per filter** (Amped docs) | **Hash-chained audit log, bit-exact re-runs, reviewer agent** §2, §19.3, §21<br>Every action chained by SHA-256; reviewer re-runs every chain twice and every analysis from the originals. | Lucida broader | — |
| Guided enhancement | **Assistant: scripted recipes, one click per filter, batch-run on folders** (Amped docs) | **Enhancement planner** §19.2<br>Builds 3–8 candidate chains and ranks them by Tenengrad sharpness, contrast, Immerkær noise and clipping. | Equivalent | Different approach: Amped scripts a fixed order, Lucida proposes and ranks. |
| AI labelling | **AI notice banner on AI filters (2026)** (Amped docs) | **Generative-step controls** §10<br>Watermark, measurement refused, logged justification, reviewer flag; model weights hash-checked. | Lucida broader | — |
| GPU use | **GPU for the diffusion detector; NVENC for video export (Amped warns it can break bit-exactness)** (Amped docs) | **GPU for learned models only after a bit-exact probe** §21<br>CPU parallelism split into independent pieces so results never depend on threads. | Equivalent | — |

## Sources

- Lucida's measured accuracy: `docs/validation/README.md` on main, and `docs/validation/deblur-benchmarks.md` for the deblur filters.
- Lucida: `docs/algorithms.md` and the filter and analysis modules in `backend/forensic/` on main (liorongoi/orensic-workbench).
- Amped research in the project folder: `amped-video-findings.md`, `research/02-features-and-performance.md`, `research/03-authenticate.md` and captions of 42 Amped videos in `research/05-youtube-captions.md`.
- [Amped Authenticate product page](https://ampedsoftware.com/authenticate) (filter list by category)
- [Authenticate update: ADJPEG, NADJPEG, Ghost map, Fusion map (MAVEN project)](https://blog.ampedsoftware.com/2015/09/17/amped-authenticate-update-new-filters-batch-processing-and-automatic-warnings)
- [Authenticate 11362: JPEG dimples (Agarwal and Farid 2017), JPEG HT, social media identification](https://blog.ampedsoftware.com/2018/07/05/amped-authenticate-update-11362-jpeg-dimples-improved-jpeg-ht-social-media-identification-and-much-more)
- [Authenticate 8782: PCE for PRNU, blocking artefacts, saturation map, LCD recapture](https://blog.ampedsoftware.com/2016/12/13/amped-authenticate-update-8782-enhanced-prnu-lcd-recapture-detection-and-many-more-filter-improvements)
- [PRNU-based forgery localisation (block-wise PCE)](https://blog.ampedsoftware.com/2018/01/17/improved-prnu-based-forgery-localization)
- [Forensic Focus review of Authenticate](https://www.forensicfocus.com/?p=12997) (Ghost map with Kolmogorov-Smirnov test, noise map settings, QT database size)
- [FIVE: nonlinear deblurring and motion deblurring with replicas](https://blog.ampedsoftware.com/2014/06/12/amped-five-update-reports-in-pdf-and-doc-new-deblurring-modes-and-more); [variable motion deblurring](https://blog.ampedsoftware.com/2021/11/09/how-to-use-variable-motion-deblurring-in-amped-five)
- [FIVE super resolution](https://blog.ampedsoftware.com/2020/01/21/how-to-improve-image-super-resolution-in-amped-five); [frame averaging](https://blog.ampedsoftware.com/2020/06/23/trading-time-for-definition-learn-how-to-master-amped-fives-frame-averaging-filter); [FIVE filter list (turbulence deblurring, component separation)](https://blog.ampedsoftware.com/2016/06/10/amped-five-update-more-than-100-filters)
