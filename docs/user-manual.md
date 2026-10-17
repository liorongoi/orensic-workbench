# Lucida user manual

This manual explains how to operate Lucida, version 0.3: starting it, managing
cases and evidence, enhancing images and video, authenticating them, measuring, reviewing
and reporting. It is written for examiners. Installation is covered in
[deployment.md](deployment.md); how each algorithm works and why it was chosen is in
[algorithms.md](algorithms.md).

Contents

1. [Before you start](#1-before-you-start)
2. [Starting Lucida](#2-starting-lucida)
3. [The screen](#3-the-screen)
4. [Cases](#4-cases)
5. [Evidence](#5-evidence)
6. [Viewing images and video](#6-viewing-images-and-video)
7. [Enhance](#7-enhance)
8. [Enhancement recipes](#8-enhancement-recipes)
9. [Authenticate](#9-authenticate)
10. [Measure](#10-measure)
11. [Audit and independent review](#11-audit-and-independent-review)
12. [Reports](#12-reports)
13. [Working with several examiners](#13-working-with-several-examiners)
14. [Connecting an AI agent](#14-connecting-an-ai-agent)
15. [Limits and troubleshooting](#15-limits-and-troubleshooting)
16. [Keyboard and mouse reference](#16-keyboard-and-mouse-reference)
17. [Filter and analysis reference](#17-filter-and-analysis-reference)
18. [Glossary](#18-glossary)

---

## 1. Before you start

Five rules shape how Lucida behaves. Knowing them explains most of what you will see.

1. **Originals are never changed.** When you add a file it is hashed (SHA-256 and MD5), stored
   read-only, and never opened for writing again. Everything you do produces new results
   linked to the original.
2. **Everything is recorded.** Every action is written to the case's audit log under your
   account's full name, in a hash-chained log that shows if anyone edits it.
3. **The preview is not evidence.** What you see while editing is a live preview. A result
   becomes evidence only when you **Render** it: the saved chain is run from the original and
   its output hash is recorded.
4. **Every result can be reproduced.** Any rendered result can be re-run from the original
   and must come out identical, pixel for pixel. The report says whether it did.
5. **Authentication results are worded "consistent with".** No single analysis proves an image
   is authentic or manipulated. Each result shows what was measured, the threshold, how
   accurate the method was in validation, and how to read it.

---

## 2. Starting Lucida

**On Windows** (installed with the offline installer): open **Start > Lucida**. A
console window opens showing the address and the log, and your browser opens Lucida.
Keep the console window open while you work; closing it stops Lucida. The Start menu
also has **Self-test** (checks the installation), **Case data folder** (opens the folder
where cases are kept), and this **User manual** and the **Algorithm guide**.

**Help inside Lucida.** Click **Help ▾** in the top bar and choose **User manual**, **Algorithm
guide** or **Installation and settings**. The guide opens in a new browser tab, so your work stays
as it was. The sign-in page has a **User manual** link too. The guides are built into Lucida, so
they work offline and always match the installed version. On the Help page, click a guide on the
left, type in **Find a section** to narrow its contents, and use the browser's find (Ctrl+F, or
⌘F on a Mac) to search the text.

**From a command line or Docker**, see [deployment.md](deployment.md). Then open
`http://127.0.0.1:8000` in Chrome, Edge or Firefox.

**First run: the administrator account.** The first time anyone opens a fresh installation,
Lucida asks for the **administrator** account: a user name (2 to 32 lower-case letters,
digits, dots, dashes or underscores), your full name (it appears in the audit log and reports),
and a password of at least 8 characters, typed twice. Click the button and you are signed in.
The administrator then adds the other examiners (see 13.1).

**Signing in.** After that, Lucida opens on a sign-in page. Type your user name and
password. A session lasts 12 hours; after that, or after **Sign out**, you sign in again. Five
wrong passwords in a row lock that user name for one minute.

**Your account.** Your full name is shown at the top right. Every action you take is recorded in
the audit log under that name; Lucida takes it from the signed-in account, so nobody can
act under another examiner's name. Click your name for **Change password** and **Sign out**
(and **Users**, if you are an administrator).

**Where you can sign in.** Unless the administrator turned on lab-network access, Lucida
answers only on the computer it runs on (address `127.0.0.1`), so every examiner signs in at that
computer. With lab-network access on, examiners can also sign in from their own PCs at
`https://<computer name>:<port>` (see 13.4).

---

## 3. The screen

The case list is the first page. When you open a case the screen has four parts:

```
+----------------------------------------------------------------------------------+
| ◆ Lucida / Case name                   Report ▾  Help ▾  Jobs 1 2   Your name ▾  |
+--------------+-----------------------------------------------+-------------------+
| Evidence   ‹ |  label · size · zoom   Crop  Fit  1:1  Orig.  |  Enhance |       › |
| [Triage]     |                                               |  Authenticate |   |
| [+ Add]      |                                               |  Measure | Audit  |
|              |              image or video frame             |                   |
| drop zone    |                                               |  tools for the    |
| evidence     |                                               |  selected tab     |
| list         |                                               |                   |
|              |  x, y · R G B                                 |                   |
| ✓ Verify     |  Frame strip (video only)                     |                   |
| ⤓ Original   |                                               |                   |
+--------------+-----------------------------------------------+-------------------+
```

- **Top bar**: the case name, the **Report ▾** menu (see 12), the **Help ▾** menu (this manual
  and the other guides, see 2), the **Jobs** list (see 13.2) and your account menu.
- **Left panel**: the evidence in the case, with buttons to add files and triage them all.
- **Centre**: the viewer, with the frame strip underneath for video.
- **Right panel**: four tabs. **Enhance** builds processing chains. **Authenticate** runs
  analyses. **Measure** takes heights, distances and speeds. **Audit** shows the log and runs
  the independent review.

To make room for the image, hide a side panel with **‹** (left) or **›** (right) at its top
corner. A thin bar stays at the edge; click it to bring the panel back. Lucida remembers
which panels you hid.

Many buttons are icons or short words. Hover any button to see what it does, and hover the **ⓘ**
marks for explanations.

---

## 4. Cases

A case groups the evidence, enhancement chains, analyses, measurements and audit log of one
investigation.

- **Create a case**: on the case list, type a name or reference and click **Create case**.
  The case opens.
- **Open a case**: click its row in the list. The address in the browser (`#/case/...`) can be
  bookmarked.
- **Return to the case list**: click the **Lucida** logo at the top left.

Cases cannot be renamed or deleted from the interface in this version: the case and its audit
log are kept as the record of the work. Case data lives in the case data folder chosen at
installation.

---

## 5. Evidence

### 5.1 Adding files

Click **+ Add** and choose one or more files, or drag files anywhere onto the evidence panel. Each file is
hashed while it is copied in; the panel shows "Hashing and storing..." until it is done. The
new item is selected when it finishes.

Accepted: still images that Pillow or OpenCV can read (JPEG, PNG, TIFF, BMP, GIF, WebP), video
that FFmpeg can decode (MP4, MOV, AVI, MKV, TS, WMV, raw H.264/H.265 and many more), and
recorder exports (see 5.3). Playlists (HLS, DASH) and concat lists are refused, because they
make the decoder read other files.

Each item in the list shows:

- a type badge: **IMG** (still image), **VID** (video) or **REC** (recorder export that needs
  converting);
- the filename and its pixel size, and for video the frame count;
- a coloured light once it has been triaged (see 9.1);
- "copy" if it is a working copy made by converting another item;
- a **🗑** button to remove it from the case (see 5.4).

Hover an item to see its size, when and by whom it was added, and its full SHA-256 and MD5.

Click an item to select it. The viewer, the frame strip and the right-hand tabs then work on it.

### 5.2 Verifying and downloading originals

With an item selected, the buttons under the list are:

- **✓ Verify**: re-hashes the stored original and checks it is still read-only. The result
  is shown and logged.
- **⤓ Original**: saves an exact copy of the original file.

### 5.3 Recorder (DVR) exports

Many recorders export video in vendor containers that ordinary players cannot open. When
Lucida recognises one or cannot open a file directly, a **Recorder export** panel appears under
the evidence list.

Formats Lucida recognises:

| Format | Typical files | Read by |
| --- | --- | --- |
| Dahua DAV (also Amcrest, Lorex, IC Realtime and other Dahua OEMs) | `.dav` | Lucida's own parser, or FFmpeg |
| Hikvision IMKH + program stream (also Annke, LTS, Swann and other Hikvision OEMs) | `.mp4`, `.264`, `.ps` | Lucida's own parser, or FFmpeg |
| Xiongmai / XMEye / NetSurveillance (many unbranded DVRs) | `.h264`, `.h265` | Lucida's own parser |
| HiSilicon IP cameras (HXVS / HXVT) | `.264`, `.265` on SD cards | Lucida's own parser |
| MPEG program stream (exacqVision and many others) | `.ps`, `.mpg`, `.vob` | Lucida's own parser, or FFmpeg |
| Older Hikvision (4HKH header) | `.264`, `.mp4` | carve |
| AVI with a vendor FourCC or per-frame vendor headers (Hikvision HKVI, Geovision GMP4, Mace and other DVRs) | `.avi` | Lucida's own parser (FFmpeg reads the standard ones) |
| MPEG transport stream, including vendor stream types and captures with lost packets; M2TS | `.ts`, `.m2ts`, `.mts` | FFmpeg, or Lucida's own parser for the loss and timing check |
| Network capture of a camera's or recorder's RTSP stream (Wireshark, tcpdump) | `.pcap`, `.pcapng` | Lucida's own parser: H.264, H.265 or MPEG-PS over RTP, by UDP or inside the RTSP connection |
| MP4/MOV, ASF, Matroska | | FFmpeg |
| Raw H.264, H.265, MPEG-4 Part 2 and MPEG-2 video streams | `.264`, `.h264`, `.265`, `.m4v`, `.m2v` | FFmpeg, or carve |
| Anything else holding H.264, H.265, MPEG-4 Part 2, MPEG-2 or JPEG frames | | carve |

Some formats are closed or encrypted and have no public description: Avigilon `.avk`, Genetec
`.g64` / `.g64x`, Samsung/Hanwha `.sec`, Dedicated Micros `.nvf`, Verint `.vxg` and self-playing
`.exe` exports. Lucida refuses them with a message saying how to export a standard file from the
vendor's software instead.

For a network capture, the working copy's clock comes from the capture computer, so it includes
network delay and that computer's clock error; the frame spacing comes from the camera's RTP
timestamps. Lost RTP packets are counted and the frames around them may show damage.

1. Read the identification line (for example "Identified as Dahua DAV · carries H264 video").
2. Choose a method:
   - **parse** (offered when Lucida has its own parser for the format, and chosen by default
     unless the file is an AVI or transport stream that already plays) reads the vendor file
     record by record. Every compressed frame stays
     byte-identical and keeps the recorder's own timing: per-frame times, the recorder clock and
     the frame rate it wrote. Gaps (missing frames) and timestamps that go backwards are listed.
     For Dahua files holding several cameras, the first channel is extracted and the others are
     named in the record.
   - **remux** copies the video stream into a standard MPEG transport stream with FFmpeg,
     without re-encoding. Use it when there is no parser, or to compare with parse.
   - **carve** extracts the raw H.264, H.265, MPEG-4 Part 2, MPEG-2 or Motion JPEG stream from
     an unknown container. It has no timestamps, so enter the recorder's frame rate; frame times
     are then computed from it.
3. Click **Make working copy**.

The working copy is added as a new evidence item, linked to the original, with the method, the
command and the FFmpeg version recorded. A copy made by parse shows a **Recorder timing**
panel: frame count, measured and recorded frame rates, the recorder clock span, gaps and
timestamp anomalies. The frame list then shows each frame's recorder time. Work on the working
copy; the original stays as it was.

### 5.4 Removing evidence

If you added a file by mistake, or it turns out not to belong to the case, remove it:

1. Click **🗑** on its row in the evidence list.
2. Optionally type a reason (up to 500 characters). It is logged.
3. Click **Remove** (or press **Enter**). **Cancel** or **Esc** keeps it.

Removing hides the item; it does not delete anything. The original file stays in the write-once
store with its hashes, the removal is written to the audit log with your name, the time and the
reason, and the report lists removed items with that information in its evidence section.
Working copies made from the item, and its enhancement chains, are hidden with it.

To bring an item back, click **Show removed (n)** at the bottom of the evidence panel, then
**Restore** next to it. Hover a removed item to see who removed it, when and why. Restoring is
logged too.

Removing evidence is a human action: an AI agent cannot remove or restore evidence.

---

## 6. Viewing images and video

### 6.1 The viewer

- **Zoom**: scroll the mouse wheel; zoom follows the cursor. From 2 % to 6400 %.
- **Pan**: drag with the mouse.
- **Fit** (or key **F**): fit the whole image in the viewer.
- **1:1** (or key **1**): one image pixel per screen pixel.
- Above 100 % pixels are drawn as sharp squares (no smoothing), so you see the real pixels.
  At 1600 % and above a pixel grid is drawn.
- **Pixel inspector**: the bar under the image shows the x, y position and the R, G, B values
  of the pixel under the cursor, with a colour swatch.
- **Compare with the original**: once a chain has steps, three buttons at the top right of the
  viewer put the original next to the result. Click the lit button again to turn it off.
  - **Swipe**: the original on the left, the result on the right; drag the yellow divider.
  - **Side by side**: two panes that zoom and pan together.
  - **Flip**: one image at a time; press **T** or click the label at the bottom to switch.

  Hold **B** at any time to peek at the original. If the chain changed the image size, the
  original is scaled onto the result so both line up (after a crop it is scaled to the same
  height). The pixel bar reads the original's own pixel when the cursor is over it.
- **▦ MB** (or key **M**, video only): how the encoder coded each block of the frame (see 6.3).
- **⛶ Crop** (or key **C**): draw a crop box on the image (see 7.5).
- **? shortcuts** at the bottom right lists the keyboard shortcuts when you hover it.

The label at the top left says what you are looking at: the result at the selected step, the
source, or the name of an analysis map.

### 6.2 Video: the frame strip

For video, the frame strip under the viewer shows:

- **◀ ▶** buttons for one frame back and forward (or keys **J** and **L**; hold **Shift** to
  move 10 frames), and a slider to scrub;
- the frame number, its type (**I**, **P** or **B**), its timestamp and its size in bytes;
- a coloured bar of every frame's type underneath: red I-frames (full pictures, usually the
  best quality), blue P-frames and green B-frames (hover the bar for this legend). Click the
  bar to jump.
- **[ In** and **Out ]**, which set the first and last frame of the selected enhancement
  chain's range (see 7.6). The range is shaded in yellow.

Frames are selected by their position in the decoded stream, so frame 1234 is always the same
frame.

### 6.3 Video: macroblocks, QP and motion vectors

For H.264, MPEG-1/2, MPEG-4 Part 2 and H.263 video, **▦ MB** in the viewer toolbar (or **M**)
shows how the encoder coded the current frame, block by block (16×16 pixels). A bar under the
viewer picks what to draw:

- **Type** colours each block: red intra (coded from scratch), blue forward (predicted from an
  earlier frame), green backward (from a later frame), purple bi (from both), yellow direct,
  grey skip (copied without new detail). The bar shows each class's share of the frame.
- **QP** is a heat map of the quantiser: blue is a low QP (more detail kept), red a high one.
- **MV** draws the motion vectors: yellow forward, cyan backward.
- **Grid** draws the macroblock grid.

Hover a block to read its position, class and QP in the status bar. **Flip**, **Swipe** and
**Side by side** compare the overlay with the plain frame. Use it to pick the frames worth
enhancing or averaging (intra blocks and a low QP carry the most real detail) and to see which
regions were only copied from another frame. It only reads the video and changes no chain.
How the data is obtained is in the algorithm guide, section 3.3.

---

## 7. Enhance

Enhancement is done in **chains**: a source (the image, or a frame or frame range of a video)
followed by steps applied in order. A chain is saved with the case, can be edited later, and can
be rendered, verified and exported.

### 7.1 Creating and choosing chains

On the **Enhance** tab:

- **Start enhancing** (when the item has no chain yet) or **＋** creates an empty chain on the
  selected evidence (for video, starting at the current frame). Chains are named
  "Enhancement 1", "Enhancement 2", and so on.
- The drop-down lists the chains on this evidence; pick one to open it.
- **⧉** duplicates the open chain, with its unsaved edits, as "name (copy)". Use it to try a
  variation without losing the version you have.
- **🗑** removes the open chain. Optionally type a reason, then click **Remove**. The chain is
  hidden, not deleted: it stays in the case record and the removal is logged. Results already
  rendered from it remain in the audit log.
- To bring a chain back, click **Show removed chains (n)** below the chain picker, then
  **Restore** next to it. Hover a removed chain to see who removed it and why. The restored
  chain opens, and the restore is logged.

You can keep several chains on the same evidence, for example to compare two approaches.

The line at the top of the chain says whether it has **Unsaved changes**, is **Saved, not
rendered**, or is **Rendered**, with a coloured dot. Hover it to see the chain's hash and the
rendered output hash.

### 7.2 Adding, editing, reordering and removing steps

1. Click **+ Add filter**. A search box opens with the cursor in it. Type part of a filter's
   name, category or description ("plate", "noise", "blur") to narrow the list, then click a
   filter, or press **Enter** to add the first match. **Esc** or **Close** closes the list.
   With the box empty, the filters you used most recently are listed first under **Recent**,
   then every filter by category (Exposure, Colour, Geometry, Denoise, Sharpen, Deblur,
   Multi-frame, AI-assisted). Hover a filter to read what it does. Multi-frame filters are
   greyed out on still images.
2. The filter is added after the selected step (or at the end) and selected. If it needs points
   or a box on the image (Crop, Perspective correction, Stabilise and others), the viewer goes
   straight into drawing mode for it (see 7.3 and 7.5).
3. Below the step list, set its parameters. Numbers have a slider and a box; choices have a
   drop-down. The preview updates as you change them. Click the filter's name (**ⓘ**) above the
   parameters to read its description and reference; hover a parameter's **ⓘ** for its help.
4. Each step in the list has:
   - a **checkbox** to switch it off and on without removing it;
   - **↑** and **↓** to move it;
   - **✕** to remove it (or select the step and press **Del**).
5. Optionally type a **Note** (why you used this step). It is printed in the report.
6. Click **Save**. Until you save, the chain shows "Unsaved changes".

**Undo and redo**: **↶** and **↷** at the top of the chain, or **Ctrl+Z** and **Ctrl+Y**
(**Ctrl+Shift+Z** also redoes), undo and redo edits to the chain: added, removed, moved or
switched-off steps and parameter changes. A slider drag counts as one edit. The undo history covers the edits since you
opened the chain, saved or not; it starts again when you open another chain.

**Seeing each step**: click a step to see the image as it is after that step; click **Source** at
the top of the list to see the chain's starting image. This works like a history panel and is the
quickest way to check what each step does. Click the last step to see the final result.

Each step shows the size of its output (for example 1920×1080), so you can see where a crop or a
resize changed it. Badges mark steps that use several frames (**multi**), run a learned model
(**AI**), may add detail (**gen**) or were proposed by an agent (**proposed**).

### 7.3 Picking points on the image

Some parameters are points: the four corners for perspective correction, a region for
stabilisation, extra regions for face redaction. When you add such a filter, picking starts
straight away; otherwise click **Pick on image**, then click on the viewer. The points are numbered as you place them. For a fixed number of points (four corners)
picking stops by itself; otherwise press **Esc** when done. **Clear** removes them.

Points are always placed on the image as it enters that step, so the coordinates match what the
step receives.

### 7.4 Rendering, verifying and exporting

At the bottom of the chain:

- **Render** saves the chain, runs it from the original (not from the preview), stores the
  result losslessly and records its pixel hash in the audit log. Do this for any result you will
  rely on.
- **Verify** re-runs the saved chain from the original with nothing cached and
  checks the result is identical to the one recorded. It reports "Reproduced bit for bit" or "Did
  not reproduce".
- **Export PNG** downloads the rendered result as a lossless PNG. It is re-computed and checked
  against the recorded hash first, and the export is logged. For a video range it exports the
  current frame of the range.

Export is disabled while the chain has unsaved changes, has not been rendered, contains agent
proposals nobody has accepted, or contains an AI generative step without a justification.

Hover the status line at the top of the chain to see the chain's hash and, once rendered, the
output hash.

### 7.5 Cropping

1. Click **⛶ Crop** in the viewer toolbar, or press **C**.
2. Drag a box on the image. Hold **Shift** for a square. The box's size is shown as you drag.
   **Esc** cancels.
3. When you let go, a **Crop** step is added to the chain (a chain is created if there is none)
   and selected. It goes after the selected step, and the box is drawn on the image as it is at
   that point, so the crop matches what you saw.

To change an existing crop, select its step and click **Draw crop on image**, then drag a new
box; or edit **X**, **Y**, **Width** and **Height** (0 means to the edge of the image) directly.
Adding **Crop** from the filter list also starts drawing straight away.

Crop is an exact copy of those pixels; nothing is resampled.

Cropping early in a chain makes every later step faster, and is required before AI super
resolution on large images.

### 7.6 Working with video: frames and ranges

A new chain on a video starts with one frame (the current one). While the chain's range is a
single frame and you have not set it yourself, moving the frame slider moves the chain with it.

For multi-frame steps (stabilise, integrate, multi-frame super resolution, pick frame) the chain
needs a range of frames:

1. Move to the first frame and click **[ In**.
2. Move to the last frame and click **Out ]**.

A range can hold up to 600 frames. Single-image steps in a chain with a range are applied to
every frame of the range. When the range has not been combined into one image, the viewer shows
the frame of the range matching the slider position.

### 7.7 Asking the planner

The **Planner** (at the top of the Enhance tab) proposes chains for a goal:

1. Type a goal, for example "make the plate readable" or "brighten the night scene", or choose a
   workflow (plate, low light, stabilise and integrate, general).
2. Optionally click **Pick region** and click two corners around the area that matters (a plate,
   a face). The planner scores candidates on that region only.
3. For video, set a frame range on a chain first if you want multi-frame methods to be tried.
4. Click **Plan**.

The planner tries several chains of classical filters, scores each one for sharpness, contrast,
noise and clipping, and creates the best two as new chains named "Planner A" and "Planner B".
Every candidate is listed with its score; click **open proposal** to open one. The score is a
ranking aid only (hover **ⓘ how scores work**). You judge whether a proposal is fit for purpose.

Steps the planner (or any agent) adds are marked **proposed** and a banner says so. They are
excluded from findings until you save the chain and click **Accept** in that banner. You can edit, disable or remove
them first.

**Estimate blur** looks for straight-line motion blur in the region (or whole frame) and
suggests a length and angle. If it finds one, add **Deblur** with blur type "motion" and enter
those values.

### 7.8 AI-assisted and generative steps

Steps marked **AI** run a learned model; their model and its weights hash are printed in the
report. Steps also marked **generative** can add detail the camera never recorded. Today these
are **AI super resolution (single image)** and **AI enhance and enlarge (Real-ESRGAN)**. For such
a chain:

- a red banner reads "Illustrative only" (with how many steps still need a justification), and the viewer shows a banner across the image;
- rendered and exported results are watermarked "ILLUSTRATIVE ONLY";
- you cannot measure on it;
- you must write a **justification** of at least 20 characters on the step (why the
  illustration is needed and what it must not be used to show) before it can be accepted or
  exported. It is logged and printed in the report. Agents cannot write it.

For evidential enlargement from video, use **Multi-frame super resolution** instead (see 8.1).

**Choosing between the two AI enlargers.** **AI enhance and enlarge (Real-ESRGAN)** gives much
sharper, cleaner results on CCTV stills, because it was trained on images degraded the way real
cameras degrade them (blur, sensor noise, resizing, JPEG blocks); with **Scale** 1 it only cleans
the image without enlarging it. It is also the more inventive of the two: letters, digits, faces
and edges in its output may be wrong, so treat them as suggestions and never read a plate from it.
**Network** "full" gives the best quality; "compact" is about ten times faster. The older **AI
super resolution (single image)** (FSRCNN or ESPCN) is milder and invents less. Both need the image
cropped to the region of interest first (the network's 4× result may not exceed 8192 pixels on a
side), and both run on the graphics card when it reproduces results exactly (see 13.3).

**Redact detected faces** finds faces and pixelates, blurs or blanks them for disclosure copies.
It misses small, turned and covered faces: add manual regions for those with **Extra regions**,
and check every frame before release.

---

## 8. Enhancement recipes

These are starting points. Adjust parameters while watching the preview, and compare with the
original (hold **B**, or use **Swipe**, **Side by side** or **Flip**; see section 6).

### 8.1 Read a number plate from video

1. On a video, create a chain and set a range of 10 to 60 frames where the plate is visible.
2. Add **Multi-frame super resolution**: scale 2, motion "affine", and pick a **Registration
   region** around the plate. Keep **Robust (median) fusion** on.
3. Add **Non-local means** (strength about 4), then **Unsharp mask** (radius 1.5, amount 1 to
   1.5), then **Local contrast (CLAHE)**.
4. If the plate is at an angle, add **Perspective correction** last and click its four corners.
5. Render, verify, export.

Alternative: **Stabilise / register frames** (affine, region on the plate) followed by **Frame
integration** (sigma clip). Or ask the planner with the goal "make the plate readable" and a
region on the plate.

### 8.2 Brighten a dark scene

1. If the footage looks grey and washed out (blacks are dark grey, whites are light grey), add
   **Video range to full range** first. Many recorders store black as 16 and white as 235, and
   players often show that without stretching it. Skip this if the image already has true blacks;
   it clips footage that is already full range.
2. If the camera and scene are still, set a frame range and add **Stabilise** then **Frame
   integration** (mean); this removes much of the noise before brightening. If things move in the
   scene, use **Video denoise (motion-compensated)** instead (see 8.7).
3. For a scene lit unevenly (a street lamp, headlights, a lit doorway and dark surroundings), add
   **Low-light enhancement (illumination map)**. It lifts the shadows a lot and the bright areas
   hardly at all, and denoises where it brightened most. Lower **Strength** for a gentler result;
   lower **Largest brightening** if the darkest areas turn to coloured noise.
4. For a scene that is evenly dark, add **Non-local means** (strength 6 to 10), then **Levels** with
   gamma 1.6 to 2.2, or **Local contrast (CLAHE)** with clip limit 2 to 3.

The planner's "low light" workflow now tries the low-light enhancement among its candidates.

### 8.3 Remove motion blur

1. Use the planner's **Estimate blur** on a region with the blurred object.
2. Add **Deblur (Wiener deconvolution)**, blur type "motion", with the suggested length and angle.
   Raise the noise-to-signal ratio if the result is grainy or rippled.
3. For a plate or text, try **Deblur (total-variation deconvolution)** with the same blur values.
   It gives much less ringing around edges and less noise than Wiener. Start with
   **Regularisation** 0.002; raise it (up to about 0.005) if noise or ripples remain, lower it if
   the result looks flat or cartoon-like.
4. For dark, noisy footage try **Deblur (Richardson-Lucy)**, with 15 to 30 iterations and a
   total-variation weight of 0.002 to 0.01.
5. If the blur is not a straight line (camera shake), choose blur type **streak** and pick two
   corners of a box around the smear of a small light (a lamp, a reflection) on a dark background;
   it is read as the blur itself. With no such light, choose **path** and click along the blur's
   trajectory, or use **Deblur (blind, estimated kernel)** with a region that has clear edges and a
   kernel size a little larger than the smear.
6. If the blur changes across the frame, use **Deblur (motion that varies across the frame)** and
   click both ends of a blur streak at two or more places.
7. For shimmer from heat or long distances, select a frame range of a fixed camera and add
   **Turbulence removal**.

### 8.4 Interlaced CCTV

Add **Deinterlace** in "field" mode first, before any other step. Each frame then shows a single
moment in time. Then add **Correct aspect ratio** if the footage is stored with non-square pixels
(for example 704×576 shown as 4:3).

### 8.5 Wide-angle lens

Add **Lens distortion** and adjust **k1** (negative for barrel distortion) until straight edges
in the scene look straight. Do this before measuring.

### 8.6 Screen moiré or interference lines

Add **Periodic noise removal (FFT)**. Lower the peak threshold to remove more of the pattern; raise
the protected centre radius if real detail is being lost.

### 8.7 Noisy video with movement

1. Set a frame range on the chain, a few frames longer than the part you need.
2. Add **Video denoise (motion-compensated)**. It follows the motion between each frame and its
   neighbours and averages each pixel with the same point of the scene in the other frames, so it
   removes far more noise than single-image denoising without smearing moving objects. Stabilising
   first is not needed.
3. **Frames used per output frame** (default 5) sets how many neighbours are averaged; more removes
   more noise but is slower. Raise **Match tolerance** for very noisy footage; lower it if moving
   edges look doubled. **Final denoise strength** adds light non-local means at the end (0 turns
   it off).
4. Frames at the start and end of the range have fewer neighbours, so they are noisier.

The result is still a range of frames, so you can add other steps after it, or **Pick frame** to
take one.

---

## 9. Authenticate

The **Authenticate** tab runs analyses on the selected item. Each analysis reads the original
file, never a preview. Results are stored, hashed and logged.

### 9.1 Smart Report and Triage

- **Smart Report** (top of the tab) runs a fixed set of checks on this item: file structure and
  metadata, provenance and clone (copy-move) detection; for JPEGs also the quantisation-table
  lookup, the two double-compression analyses and JPEG dimples; for video, frame
  integrity and double encoding. It sorts the item **red** (a local or structural inconsistency
  to examine first), **yellow** (a processing history, such as re-saving or editing software,
  that limits what can be said) or **green** (no check found an inconsistency). Each reason is
  listed; hover it for the detail.
- **Triage** (top of the evidence panel) runs the Smart Report on every item in the case, colours the light
  next to each item and writes a summary as a case note, labelled agent opinion.

A colour tells you where to look first. Green is not a finding that the item is authentic.

### 9.2 Running an analysis

The analyses that apply to the item are grouped in stages, in the order an examination usually
follows: **File**, **Global**, **Camera**, **Local**, **AI / deepfake**, **Geometric** (and
**Other**). Each stage heading shows how many of its analyses have been run ("2/5 run") and a
light: red if any of them raised a warning, green if those run found none. Click a heading to
fold or unfold the stage.

Each analysis has one row with:

- a light: red (its latest run raised a warning), green (ran without a warning) or grey (not run);
- its name, an **AI** badge if it uses a learned model, and once run a one-line verdict. Hover
  the name for its description and validated accuracy (from the detector card);
- **⚙** to open its parameters;
- **Run**, or **Re-run** once it has run.

Click a row that has been run to show its map in the viewer. Every run is listed under
**History**, newest first; click one to open it, and its first map (if any) is shown in the
viewer. Each result shows:

- a one-line summary;
- flags: warnings (amber) are inconsistencies worth explaining; information (blue) is context;
- maps: click a thumbnail to show it in the viewer; **Back to image** returns;
- **How to read this** (click to open): the reading guide for this analysis;
- the findings: the numbers behind the summary;
- the analysis version, who ran it and the result hash.

### 9.3 The analyses

| Analysis | Use it to | Read it |
|---|---|---|
| File structure and metadata | See EXIF, GPS, XMP, JPEG markers, quantisation tables, thumbnail; for video the container, frame types and GOP. | Warnings are inconsistencies to explain (editor software, date mismatch, thumbnail not matching, trailing data). Metadata is easy to edit, so it can support but never prove authenticity. |
| Quantisation tables vs camera database | List the cameras and programs known to write exactly this JPEG's tables, and check them against the camera named in EXIF. | A match narrows the source, never proves it. A mismatch with the EXIF camera suggests re-saving, unless the camera used a quality the database lacks (the number of table sets held for it is shown). In testing, about one genuine camera original in four gave such a false mismatch, so treat a mismatch as a reason to look further, not as a finding. Add your own reference images with `python -m forensic.qtables add <folder> --source "<name>"`. |
| Content credentials and AI provenance | Validate C2PA Content Credentials and find declarations of AI generation. | A declaration of AI generation is strong evidence. No credentials is normal and means nothing. |
| Error Level Analysis (ELA) | Quick look for areas compressed differently. | Edges and texture are bright in authentic images too. Compare like with like. Confirm with other analyses. |
| JPEG Ghost | Find a region saved earlier at a lower quality. | A compact dark region at a quality where the rest is bright. |
| Aligned double JPEG (A-DJPG) | Test if the JPEG was saved twice, estimate the first quality, map regions without that history. | The map means something only if the global test is positive. Look for a compact bright region over textured content. |
| Non-aligned JPEG grid (NA-DJPG) | Detect cropping between saves, or a region from another JPEG. | A secondary grid over the whole image suggests cropping; in one region, a pasted region. Brick, fences and text can mimic a grid. |
| Clone (copy-move) detection | Find an area copied from one part of the image to another, to hide or duplicate something. | Each copy is outlined (strong colour one side, light colour the other) with lines joining matching points, and its rotation, scale and shift are listed. Repeated patterns (tiles, windows, fences, text, symmetric objects) match naturally: judge each match against the scene. No match does not show the image is unedited. Part of the Smart Report for still images. |
| Noise level map | Find areas whose fine-grain sensor noise is clearly stronger or weaker than elsewhere at the same brightness (pasted, smoothed or retouched areas). | Dark means less noise, bright yellow more; hatched areas cannot be measured. A compact area with different noise whose outline follows an object is worth a closer look; gradual changes across the frame are natural. A visual aid: its outlines are information, not warnings, and it is not part of the Smart Report. |
| JPEG dimples | Find the faint mark many camera encoders leave in every 8x8 block, and areas that lack it. | A dimple (strength 13 or more) shows the image passed through a camera encoder that makes them; it often survives re-saving, so it does not prove the file is the original. Only about one camera original in ten has a dimple, so absence alone means nothing. Bright areas in the map lack the dimple the rest carries (pasted or retouched); hatched areas are too flat to tell. |
| Luminance gradient (LGA) | See which way brightness changes everywhere (colour = direction, brightness = strength). | Compare surfaces that should be lit alike; a pasted object often shades in a direction that does not fit. Visual aid only. |
| PRNU tampering map | For an image from a camera with a reference in the case, find areas without that camera's sensor fingerprint. | Only meaningful when the whole image matches (PCE above 60, shown). Outlined areas lack the fingerprint (pasted, cloned or painted); hatched areas are too dark, flat or saturated to decide. Needs a camera reference (9.4). |
| Fusion map | One map that weighs A-DJPG, NA-DJPG, dimples and noise together. | Bright = belief of alteration; outlined areas list the analyses behind them. In testing it found about 8 in 10 pasted areas and outlined about 1 in 20 innocently re-saved images. Each input counts only when its conditions hold; open the inputs' own maps before relying on an outline. A fixed rule, not a learned model. |
| Camera identification (PRNU) | Test if the image came from a specific camera. | PCE above 60 is consistent with the reference camera. Needs a camera reference (9.4). |
| Camera-pipeline traces | Look for demosaicing traces (camera capture) and resampling (resized or rotated). | Traces present are consistent with a camera at this resolution. Their absence means little after JPEG or resizing. |
| Generated-image detector (GAN and diffusion) | Screen an image for being made entirely by an AI generator (Stable Diffusion, FLUX, DALL-E, Midjourney, GANs). Bundled; runs on the GPU when available. | Score 0.5 or more: consistent with generated; 0.2 to 0.8 is uncertain. Screening, never proof. In testing on 20 generators it had never seen, it caught 84 % of generated images with 0.4 % false alarms, but only 35-65 % for Adobe Firefly, Midjourney 6.1 and Ideogram 2, and fewer after JPEG re-saving or down-scaling. Newer generators can be missed, and a generated object pasted into a real photo is usually not detected. |
| Learned AI-image detector | Run a further detector your lab installed (for example one trained on newer generators). | Available only when installed. A score to weigh with the others, never proof. |
| Face-swap and face-edit checks | Compare each face's noise and detail with its surroundings. | z of 4 or more: the face differs more than almost any other region does. Faces under 40 pixels between the eyes give weak results. Mark faces the detector misses. |
| Shadow consistency | Test whether one light casts every shadow. Open ⚙, **Pick on image**, then click a shadow point and the object point casting it, for each shadow (at least two). | Green region: where the light can be. **Inconsistent** names the smallest set of shadows that conflict (red in the map) and the tolerance the conflict holds to; report that tolerance. Use clear shadow-object pairs only. In testing there were no false alarms on consistent scenes, and a shadow from a light 20° off was caught in 84 % of cases (about half at 10°, rarely at 5°). **Consistent** does not show the image is authentic. |
| Reflection consistency | Test reflections in one flat mirror, window or water: click an object point, then the same point in the reflection. | As for shadows; curved or rippled surfaces are not supported. |
| Perspective consistency | Test whether lines parallel in the scene (building edges, kerbs, shelves) meet at one vanishing point: click two points along each line. | One direction per run. Lens distortion bends lines; correct it first. |
| Double encoding (VPF) | Test if a video was decoded and re-encoded, and estimate the first GOP. | Re-encoding is not manipulation; it shows the file is not the recorder's original stream. |
| Frame duplication and deletion | Find repeated frames, jumps, repeated segments and timestamp gaps. | Each finding marks frames to look at. CCTV repeats frames routinely. |

[algorithms.md](algorithms.md) explains each method, its thresholds and its limits in detail.

**Marking regions for an analysis.** Some analyses take regions (for example face regions). Open
its settings (**⚙**), click **Pick on image** and click pairs of corners on the viewer; press **Esc** when
done.

### 9.4 Camera references (PRNU)

To test whether an image came from a camera you need a reference fingerprint for that camera,
built from images known to come from it.

1. Add the known images to the case (20 or more flat, bright, unsaturated images, such as sky or
   a white wall, all at the camera's full resolution and the same orientation).
2. On the Authenticate tab of any still image, under **Camera references**, click **New
   reference**.
3. Give it a name and optionally the camera make and model, tick the known images, and click
   **Build from n images**.
4. On the questioned image, open **Camera identification (PRNU)**, choose the reference, and click
   **Run**. In its settings (**⚙**) you can turn on searching for cropping (on by default) and for
   down-scaling.

---

## 10. Measure

The **Measure** tab takes measurements from points you click on the image. Every result comes with
a 95 % interval computed from your clicking uncertainty (and the survey uncertainty where it
applies), and is stored and logged.

Correct lens distortion first if the camera has a wide lens (see 8.5). To measure on a corrected or
enhanced image, tick **Measure on the result of "chain name"** (the chain must be saved, and
cannot contain a generative AI step). Otherwise you measure on the original.

For every method:

1. Choose the method from the drop-down. Read its description and, under **Assumptions and
   reference**, what it assumes.
2. For each field, click **Pick** and click on the image. Lines take two clicks each; points take
   one. **Redo** starts a field again; **+1** adds another line or point.
3. Fill in the values the method needs (below).
4. Set the **Clicking uncertainty** (how many pixels your clicks could be off; 1 is typical when
   zoomed in) and, where shown, the **Survey uncertainty** in metres.
5. Optionally give it a **Label**, then click **Measure**.

Results show the value, the 95 % interval, whether it was measured on the original or an enhanced
result, and warnings if the geometry is unstable or the control points fit poorly.

### 10.1 Height by single-view metrology

For a person or object standing on the ground, when another object of known height stands on the
same ground.

- **Vertical lines** (red): at least two edges that are vertical in the scene, such as door frames,
  poles or building corners, spread across the image.
- **Ground lines, direction 1** (blue): two lines parallel on the ground, such as kerb edges.
- **Ground lines, direction 2** (green): two lines parallel on the ground in another direction,
  such as paving joints.
- **Reference base** and **Reference top**: the bottom and top of the object of known height.
- **Target base** and **Target top**: the bottom and top of the person or object to measure.
- **Reference height** in metres and its uncertainty.

More lines in each set make the result more stable.

### 10.2 Distance on a plane

- **Surveyed ground points**: at least four points on the ground whose positions you measured at
  the scene. After picking, type each point's ground x and y in metres in the table.
- **First point** and **Second point**: the two points whose distance you want.

### 10.3 Speed (video only)

As for distance, plus:

- Move to frame A, click **Use current** next to Frame A, and pick the **Contact point in frame A**
  (for example where a tyre touches the road).
- Move to frame B, click **Use current** next to Frame B, and pick the **Same point in frame B**.

The time between the frames comes from the video's own timestamps. The result is in m/s and km/h.

### 10.4 Height by camera resection

When you surveyed at least six points in 3D at the scene (X, Y on the ground, Z up, in metres,
spread across the image and in depth):

- **Surveyed 3D points**: pick them, then type each one's X, Y and Z.
- **Target base (on the ground)** and **Target top**.

The result also shows the camera's recovered focal length and position, and how well the control
points fit (RMS in pixels). A poor fit points to a mis-clicked or mis-surveyed point, or to lens
distortion.

---

## 11. Audit and independent review

The **Audit** tab shows:

- **The hash chain status**: "✓ Log intact · n entries" or, if the log has been altered,
  "Hash chain BROKEN at entry n".
- **Independent review** (hover **ⓘ** for what it checks): click **Run reviewer** (or **Review again**). The reviewer re-hashes the
  originals, checks the audit log, re-runs every rendered chain twice from the originals, re-runs
  analyses and measurements and compares hashes, checks the AI model files, and flags generative,
  unjustified or unaccepted steps. It reports **PASS**, **WARN** (points the report must explain)
  or **FAIL** (resolve before reporting), with each item listed. Passed checks are hidden; click
  **Show passed checks** to see them. Run it before you write a report.
- **Case notes**: type a note and click **Add**. Notes are logged and printed in the report.
- **The log**: every action, newest first, with who did it (agents are marked), when, and the
  start of the output hash. Click the copy button next to a hash to copy it in full. Removals
  and restores of evidence and chains appear here with their reasons.

---

## 12. Reports

Click **Report ▾** in the top bar of an open case and choose one. Each opens in a new browser
tab.

- **HTML report**: the report for reading on screen.
- **PDF report**: the same report for printing and disclosure.
- **JSON bundle**: the complete case record with every hash, for archiving or for another tool.

The report contains, in order: the evidence received with hashes and integrity checked at report
time, and any evidence removed from the case with who removed it, when and why; each enhancement chain with every step, its parameters, references and notes, before and
after images, and whether it reproduced bit for bit; the authentication analyses with maps,
reading guides and detector accuracy; measurements with intervals and assumptions; triage results
and rules; the independent review; the audit trail; and the software environment (versions of
Lucida, Python libraries, FFmpeg and models).

Generating the report re-checks the originals and re-runs chains, so it can take a while on a large
case.

---

## 13. Working with several examiners

Several examiners can use Lucida at the same time, each signed in to their own account.
Heavy work goes through one shared job queue, so the machine is never asked to do more at once
than it can, and everyone can see what is running.

### 13.1 Accounts (administrators)

Click your name at the top right, then **Users**. The list shows every account with its full
name, user name and role.

- **Add an examiner**: at the bottom, type a user name, the examiner's full name and a first
  password (8 or more characters), choose **Examiner** or **Administrator**, and click **Add**.
  Give the examiner the password; they can change it with **Change password**. Two accounts
  cannot share a full name, because the audit log must tell them apart.
- **Change a role**: pick **Examiner** or **Administrator** in the account's row. Administrators
  can manage accounts; otherwise both roles work the same way. There must always be at least one
  active administrator, and you cannot change your own role.
- **Reset a password**: click **Reset password** and type the new one. The account's open
  sessions end, so the examiner must sign in again with the new password.
- **Disable an account**: click **Disable**. The account is signed out at once and can no longer
  sign in. Its past actions stay in the audit log under its name. **Enable** brings it back.
  Accounts are disabled rather than deleted so that the log always names a known person.

Creating accounts and changes to them are written to the audit log.

### 13.2 The job queue

These operations are queued: rendering and verifying a chain, every analysis, converting a
recorder export, the Smart Report, Triage, building a camera reference, the independent review,
and the reports (HTML, PDF and JSON bundle). When you start one, the button shows that it is busy until the job
has run, as before; if others are using the machine it may wait its turn first.

Click **Jobs** in the top bar to see the queue. The button shows two counts: jobs running and
jobs waiting. The list shows, for every examiner:

- a dot for the job's state (running, waiting, done, failed or cancelled);
- what it is (for example "Render chain", "Analysis") and what it works on, with a link to its
  case. A **GPU** badge marks jobs that run a learned model on the graphics card;
- who started it;
- for a waiting job its place in line (**#1** is next), for a running job how long it has run,
  and for a finished job how long it took. A failed job shows its error.

The header shows how many CPU and GPU places are in use (for example "CPU 3/4 · GPU 1/1") and
how many jobs are waiting. Tick **Mine** to see only your own jobs. Below the active jobs,
**Recent** lists the latest finished ones. Your own jobs are highlighted.

**Cancelling**: click **✕** on a waiting job to take it out of the queue. You can cancel your
own jobs; an administrator can cancel anyone's. A job that has already started runs to the end.

**Taking turns**: when a place frees up, the next job goes to the examiner with the fewest jobs
running, then to whoever has waited longest for a turn. If you start twenty analyses, a
colleague's single render runs as soon as the next place frees up instead of waiting behind
all of yours. One examiner can have up to 50
jobs waiting at a time.

**If Lucida is restarted**, jobs that were running are marked failed (start them again)
and jobs that were waiting run when it is back.

### 13.3 Settings for the administrator

How many jobs run at once is set in `workbench.ini` in the installation folder:

| Setting | Default | Meaning |
|---|---|---|
| `job_workers` | 4 | Jobs without a learned model (most filters and analyses) that run at the same time. |
| `gpu_jobs` | 1 | Jobs that run a learned model on the graphics card at the same time. |

On a 128 GB machine with an RTX A4000 (16 GB), `job_workers = 6` and `gpu_jobs = 1` are a
reasonable start. Raise `gpu_jobs` to 2 only if the models in use fit twice in the card's
memory. Restart Lucida after changing them. The same values can be set with the
environment variables `FORENSIC_JOB_WORKERS` and `FORENSIC_GPU_JOBS`.

Four more optional settings control how much of the machine each job may use. Leave them unset
unless there is a reason; the defaults use the whole machine.

| Setting | Default | Meaning |
|---|---|---|
| `threads` | every CPU core | Threads each filter may use. Results are identical whatever the number. |
| `cache_mb` | a quarter of RAM, at most 32 GB | Memory for intermediate results, so changing step 5 does not re-run steps 1 to 4. |
| `frame_cache_mb` | an eighth of RAM, at most 16 GB | Memory for decoded video frames, so stepping through a video does not decode it again. |
| `ai_device` | `auto` | Where the learned models run: `auto` (the graphics card when it reproduces results exactly, otherwise the CPU), `gpu` or `cpu`. |

On Windows the models run on the graphics card through DirectML, which works with the normal
graphics driver; no CUDA install is needed. Before a model is first used, Lucida runs it twice on the card and uses the card
only if both runs are identical to the bit; otherwise that model runs on the CPU. The report's
software environment section and the **Self-test** output say which device the models ran on.

Case data is shared by everyone who signs in. When installed **for all users**, the case data
folder is shared by the members of the local **Forensic Examiners** Windows group (see
[deployment.md](deployment.md)).

---

### 13.4 Using Lucida from other PCs on the lab network

Lucida can let examiners work from their own PCs, so the workstation does the heavy work while
each examiner uses a browser at their desk.

**Turning it on (administrator, once).** Either tick **Let examiners connect from other PCs on the
lab network** when installing (silent install: `/TASKS="labnetwork"`), or set `network = lab` in
`workbench.ini` and restart Lucida. Lucida then:

- serves the lab network over **HTTPS** only;
- creates, without any internet connection, its own certificate authority and a certificate for
  this computer's names and addresses, kept in the `tls` folder of the case data folder (renewed
  automatically if the names change). A lab that has its own certificate sets `tls_cert` and
  `tls_key` in `workbench.ini` instead;
- on an all-users installation, opens its port in Windows Firewall for domain and private
  networks only, never public ones. On a per-user installation Windows asks the first time
  Lucida starts.

When it starts, the Lucida window prints the lab-network address and the certificate's SHA-256
fingerprint, for example:

```
  Lab network: https://lab-ws01:8000 (examiners sign in with their Lucida accounts)
  Certificate: install ...\tls\workbench-ca.crt on each examiner PC once
               (Trusted Root Certification Authorities); SHA-256 3F:9A:...
```

**On each examiner PC (once).**

1. Copy `workbench-ca.crt` from the `tls` folder of the case data folder to the PC.
2. Check that its SHA-256 fingerprint matches the one the Lucida window prints: run
   `certutil -dump workbench-ca.crt` and compare the **Cert Hash(sha256)** line, ignoring spaces,
   colons and upper or lower case. (The certificate dialog's **Thumbprint** is SHA-1, a different
   number, and `certutil -hashfile` hashes the file's text, not the certificate.) Do not install
   it if they differ.
3. Install it: double-click it > **Install Certificate** > **Local Machine** > **Place all
   certificates in the following store** > **Trusted Root Certification Authorities**. Or, as
   administrator: `certutil -addstore Root workbench-ca.crt`.
4. Open `https://<computer name>:<port>` (the address the Lucida window prints) in Chrome, Edge or
   Firefox and sign in with your Lucida account. Bookmark it.

Everything works as on the workstation itself: the same cases, the same job queue, and your actions
recorded under your account. Uploading large videos over the network takes longer than on the
workstation.

**What protects it.** Every request needs a signed-in Lucida account; passwords and the sign-in
cookie travel only over HTTPS; Lucida answers only requests addressed to the computer's own names
and addresses. AI agents connecting over the network need a separate secret token (see
[deployment.md](deployment.md#examiners-on-other-pcs)).

---

## 14. Connecting an AI agent

An AI assistant (for example Claude Desktop, or a self-hosted model with an MCP client) can drive
Lucida through its MCP servers: evidence, enhance, authenticate, measure and case. Configuration
is in the [README](../README.md#connecting-an-agent). What an agent can and cannot do:

- It can add evidence only from the import folder set by `FORENSIC_IMPORT_DIR`.
- Steps it adds are **proposed**; you accept them in the Enhance tab.
- It cannot accept its own proposals, cannot remove, disable or reorder your steps, and cannot write
  the justification for a generative step.
- Every agent action is logged with the agent's name and model.
- In this version agents run their work directly, not through the shared job queue, so their jobs
  do not appear under **Jobs** and do not wait their turn.

---

## 15. Limits and troubleshooting

| Situation | What to do |
|---|---|
| "frame ranges are limited to 600 frames" | Use a shorter range, or several chains. |
| "the result would be wider than 8192 pixels" | Crop to the region of interest before AI super resolution. |
| A multi-frame filter says it needs a frame range | Set **[ In** and **Out ]** on a video (7.6). |
| Export PNG is greyed out | Save and render first; accept or remove agent proposals; justify any generative step. |
| "This recorder export cannot be opened directly" | Make a working copy (5.3). |
| A recorder file is refused as a closed format | Export a standard MP4 or AVI from the vendor's software, as the message says (5.3). |
| Carved video plays at the wrong speed | Re-convert with the recorder's real frame rate, or use parse if it is offered. |
| Parse fails (a "parser:" error, or the codec was not recognised) | The file may be damaged or a variant of the format. Try remux, then carve. |
| ▦ MB is missing for a video | Macroblocks are read from the decoder for H.264 and MPEG-1/2/4 Part 2 and H.263, not H.265 or Motion JPEG. |
| Double encoding says the codec is not supported | It works on H.264 and MPEG-1/2/4 Part 2 streams, not on H.265. |
| Face check finds no face | The detector misses small, turned or covered faces; mark them in its settings (**⚙**). |
| PRNU: "the image is larger than the camera reference" | Build the reference from images at the camera's full resolution. |
| The browser shows "Invalid host header" | You are reaching Lucida by a name it does not know (for example an alias or a different address). Use the address the Lucida window prints, or an administrator adds the name to `allowed_hosts` in `workbench.ini`. |
| An examiner PC says the connection is not private or the certificate is not trusted | Install `workbench-ca.crt` on that PC as described in 13.4, after checking its fingerprint, then restart the browser. |
| An examiner PC cannot reach the lab-network address at all | Check that lab-network access is on, that both PCs are on the same network, and that the network is not set to "Public" in Windows (the firewall rule covers domain and private networks only). |
| Self-test says the models run on the CPU on a machine with a graphics card | The card failed the exact-reproduction check, or its driver is missing or too old. Update the graphics driver and run the self-test again. Results are still correct on the CPU, only slower. |
| The preview says "Updating preview..." for a long time | Heavy steps on full frames are slow. Crop first, or switch heavy steps off while adjusting earlier ones. |
| "too many wrong passwords; wait a minute and try again" | Wait one minute. If the password is forgotten, an administrator resets it (13.1). |
| "this account is disabled" | Ask an administrator to enable it (13.1). |
| A button stays busy for a long time | Your job may be waiting its turn. Open **Jobs** to see its place in line. |
| "you already have 50 jobs waiting" | Cancel some under **Jobs**, or wait for them to run. |
| A job failed with "Lucida was restarted while this job ran" | Start it again. |
| Verify says "Did not reproduce" | Report it: it means the software or a model file changed. Run **Self-test** and the reviewer. |

After any installation or upgrade, run **Start > Lucida > Self-test** and keep its output
with the lab's validation records.

---

## 16. Keyboard and mouse reference

| Action | Key or mouse |
|---|---|
| Zoom in or out around the cursor | Mouse wheel |
| Pan | Drag |
| Fit to window | **F** |
| Actual pixels (1:1) | **1** |
| Show the original while held | Hold **B** |
| Draw a crop box | **C** (or **⛶ Crop**), then drag; hold **Shift** for a square |
| Stop picking points, cancel a crop, close a confirmation | **Esc** |
| Place a point | Click (without dragging) while picking |
| Undo / redo a chain edit | **Ctrl+Z** / **Ctrl+Y** (or **Ctrl+Shift+Z**) |
| Remove the selected step | **Del** |
| Add the first filter matching the search | **Enter** in the filter search box |
| Previous or next frame | **J** / **L** (or **◀** / **▶**) |
| Ten frames back or forward | **Shift+J** / **Shift+L** |

---

## 17. Filter and analysis reference

### Filters

| Category | Filter | What it does |
|---|---|---|
| Exposure | Levels | Maps an input range to an output range with a gamma curve. |
| Exposure | Brightness and contrast | Brightness offset; contrast around mid-grey. |
| Exposure | Curves | Smooth tone curve through control points that never reverses tonal order. |
| Exposure | Local contrast (CLAHE) | Lifts detail in dark and bright areas without changing colour. |
| Exposure | Histogram equalisation | Spreads lightness over the full range. |
| Exposure | Video range to full range | Stretches washed-out 16-235 "video range" footage to full black and white. |
| Exposure | Low-light enhancement (illumination map) | Lifts dark areas much more than bright ones, keeping colour; for unevenly lit night scenes. |
| Colour | Colour balance | Channel gains, or automatic grey world. |
| Colour | Greyscale / channel | Luma, or one colour channel. |
| Colour | Invert | Negative. |
| Geometry | Crop | Keeps a rectangle; exact copy. |
| Geometry | Rotate | By any angle; 90° steps are exact. |
| Geometry | Flip | Mirror horizontally or vertically. |
| Geometry | Resize | Scale with a chosen interpolation. |
| Geometry | Correct aspect ratio | Restore the display ratio of non-square-pixel footage. |
| Geometry | Perspective correction | View a flat object front-on from four corners. |
| Geometry | Lens distortion | Remove barrel or pincushion distortion. |
| Geometry | Deinterlace | Separate the two fields of interlaced video. |
| Denoise | Median filter | Removes speckle and impulse noise. |
| Denoise | Gaussian blur | Smooths. |
| Denoise | Bilateral filter | Smooths while keeping edges. |
| Denoise | Non-local means | Strong noise reduction on sensor noise. |
| Denoise | Periodic noise removal (FFT) | Removes interference patterns and moiré. |
| Sharpen | Unsharp mask | Raises edge contrast. |
| Deblur | Deblur (Wiener) | Inverts a known motion, defocus or Gaussian blur. |
| Deblur | Deblur (Richardson-Lucy) | Iterative deblurring, better on noisy low light. |
| Deblur | Deblur (total-variation deconvolution) | Deblurring with little ringing or noise; good on plates and text. |
| Deblur | Deblur (blind, estimated kernel) | Estimates an unknown blur (camera shake) from the image, then removes it. |
| Deblur | Deblur (motion that varies across the frame) | Motion blur that changes across the image, from streaks you draw. |
| Multi-frame | Stabilise / register frames | Aligns frames to a reference. |
| Multi-frame | Frame integration | Averages aligned frames to cut noise. |
| Multi-frame | Pick frame | Takes one frame from a range. |
| Multi-frame | Turbulence removal | Steadies heat shimmer: aligns each frame locally, fuses, removes residual blur. |
| Multi-frame | Multi-frame super resolution | Higher resolution from several shifted frames; evidential. |
| Multi-frame | Video denoise (motion-compensated) | Averages each frame with its motion-aligned neighbours; keeps moving objects sharp. |
| AI-assisted | AI super resolution (single image) | Enlarges with a small neural network (FSRCNN/ESPCN); generative, illustrative only. |
| AI-assisted | AI enhance and enlarge (Real-ESRGAN) | Cleans and enlarges 1-4x, much sharper on CCTV but invents more; generative, illustrative only. |
| AI-assisted | Redact detected faces | Pixelates, blurs or blanks faces for disclosure. |

### Analyses

See the table in section 9.3.

---

## 18. Glossary

- **Chain**: an ordered list of enhancement steps applied to a source.
- **Render**: run a saved chain from the original, store the result and record its hash.
- **Pixel hash / output hash**: a SHA-256 fingerprint of the result's pixels; identical pixels give
  the identical hash.
- **SHA-256, MD5**: fingerprints of a file. Any change to the file changes them.
- **Audit log**: the record of every action, each entry linked to the one before by its hash.
- **Proposed step**: a step added by an agent, excluded from findings until an examiner accepts it.
- **Generative step**: a step that can add detail the camera never recorded; illustrative only.
- **Working copy**: a standard video file made from a recorder export without re-encoding.
- **I, P, B frames**: a full picture (I), a picture predicted from earlier frames (P), or from earlier
  and later frames (B).
- **GOP**: group of pictures; the number of frames from one I-frame to the next.
- **PRNU**: the sensor's fixed noise pattern, used to identify an individual camera.
- **PCE**: peak-to-correlation energy, the PRNU match score.
- **C2PA / Content Credentials**: a signed record of where a file came from and how it was edited.
- **Detector card**: the published validation results of an analysis: thresholds, detection and
  false-alarm rates, and known failure modes.
- **95 % interval**: the range a measurement falls in with 95 % probability given the stated
  clicking and survey uncertainty.
