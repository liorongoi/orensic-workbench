# Still-image traffic detection

Counts and reviews vehicles in still images from stationary and mobile traffic cameras, by day and
night, with a per-image record (name, local time, number of vehicles, review status), examiner
corrections that are kept for training, and training that adapts the detector to each camera.

Open a case, add the camera's images as evidence, then click **Traffic** in the top bar (or
*Tools → Traffic detection (still images)*). The page is `#/case/<id>/traffic`.

## Workflow

1. **Cameras.** Every still image in the case gets a traffic record the first time the page opens.
   Images with no camera go to the case's first camera (created as "Camera 1" if there is none).
   Add more cameras with **+** and move an image to another camera under the picture.
2. **Local time.** Enter the camera's UTC offset in hours ("Local time = UTC + 3"). The capture
   time comes from EXIF `DateTimeOriginal` (converted to UTC when `OffsetTimeOriginal` is present),
   otherwise from a date and time in the file name (`20240501_231500`, `2024-05-01 23-15-00` …);
   without an offset tag the camera clock is taken as UTC. *set time* under the picture enters a time
   by hand. The source of every time is kept and printed in exports and reports.
3. **Regions.** *Draw region* → click the corners → **Close polygon** (or Enter). A *mask* means
   "never count a vehicle here" (a car park, a billboard); a *scan area* means "count only here".
   Regions belong to the camera, or to one image only. A box counts when its bottom centre (where
   the vehicle meets the road) lies in a scan area and outside every mask.
4. **Detect.** *Detect new images* runs every image not yet processed, in the background; the list
   fills in as it goes. *Re-run unedited* repeats detection on images that were never edited or
   approved. One image: **Detect / Re-run** under the picture.
5. **Review.** Select a box to move it or drag a corner to resize it; change its class; press
   **Delete** (or *False alarm*) to mark a false alarm; *Add missed* and drag a box around a vehicle
   that was missed. **Approve** (key A) when the image is right. N / P go to the next / previous
   image. *Show below-threshold* draws what the detectors proposed but did not count.
6. **Background.** *Learn background* estimates, from the camera's own images (at least 3 of the same
   size and lighting), the empty scene and the map of scenery lights. Run it again as images are
   added.
7. **Training.** Training starts by itself after *Train after N corrections* (default 20) new
   corrections on a camera, or with **Train now**. A notice appears when it starts and when it
   finishes, with the measured change. When the new profile is adopted, detection re-runs only on
   images that were **detected but never edited or approved**; reviewed images keep exactly what the
   examiner left.
8. **Output.** Excel (sheets *Images*, *Detections*, *Corrections*, *Training*, *Method*), CSV, JSON,
   a *Training set* (reviewed images and boxes in YOLO format, for fine-tuning a detector elsewhere),
   and the traffic report (HTML or PDF) with the charts drawn on the page.

## Charts in words

Type what you want to see and click **Draw**:

    <what> by <dimension> [and <dimension>] [as bar|line|pie|stacked] [for <filters>]

- *what*: detections (default), images, false alarms, missed, corrections
- *dimension*: hour, date, weekday, camera, lighting, status, class
- *filters*: day, night, twilight, night_ir; approved, edited, detected; `camera <name>`;
  `from 2024-05-01`, `to 2024-05-31`

Examples: `detections by hour and lighting as stacked`, `images by status as pie`,
`false alarms by camera for night`, `detections by date for camera North gate from 2024-05-01`.
Each chart says how the request was read and lists any words it did not understand. The charts on
the page go into the report.

## How detection works

`backend/forensic/traffic/`

| Step | Module | Method |
|---|---|---|
| Lighting | `scene.py` | Fixed, versioned rules on mean luminance, share of dark pixels and colour (`RULES`): day, twilight, night, night_ir (monochrome infrared). The examiner can override any image. |
| Visibility indicators | `scene.py` | Haze/fog from the dark-channel prior (He, Sun and Tang 2009) and low contrast; snow from bright unsaturated pixels in the lower half. Indicators only. |
| Pre-processing | `scene.py` | On a working copy fed to the detectors only: CLAHE at night and twilight, dark-channel dehazing in haze. Recorded with parameters. |
| Learned detector | `detectors.py` | YOLOX-S (COCO, Apache-2.0, bundled, hash-pinned) on the whole picture and, for pictures larger than about 900 px, on overlapping full-resolution tiles so that distant vehicles reach the network at a usable size (slicing-aided inference). People are detected too, only so that the other members never count a pedestrian. |
| Vehicle lamps | `detectors.py` | Bright compact blobs; pairs of similar blobs side by side are headlights (white) or tail-lights (red), with a body box above them. A single lamp (both headlights merged at long range) is proposed with a low score. Lamps where the camera shows scenery lights are dropped. |
| Change | `detectors.py` | Difference in Lab against the camera's learned empty scene, robust threshold (median + 6 MAD), compact regions. |
| Background | `background.py` | Per camera and lighting: per-pixel median of the camera's images, and for each pixel the share of images in which it is part of a lamp blob. Pixels lit in at least half of the images are scenery lights. A lamp false alarm the examiner deletes is added to the camera's scenery lights at once. |
| Ensemble | `ensemble.py` | Candidates that overlap (IoU ≥ 0.3, or a lamp inside a vehicle box) are one object. Score = 1 − ∏(1 − wₘ·sₘ) over members (weights 1.0 / 0.7 / 0.45); box from the most reliable member present. Counted when the score ≥ the profile threshold (default 0.4) and the anchor is in the regions. |
| Training | `learn.py` | Ground truth = the boxes left on edited or approved images. Every fused object of those images (counted or not) is labelled by IoU ≥ 0.5 with a final box. A logistic re-scorer over 12 features (member scores, size, position, lighting, brightness, edges) is fitted by Newton-Raphson, and the threshold is chosen for the best F1 over image-grouped cross-validation. Adopted only if its cross-validated F1 is at least the current profile's on the same images. Vehicles added by hand that no member proposed cannot be learned this way; they are counted as *unrecoverable misses* and kept for detector fine-tuning. |

Each run records the image hash, every setting, the profile and its hash, the model and its weights
hash, the scene analysis, what each member found, every fused object (with the ones below threshold)
and an output hash. The same image, settings and profile give the same output hash.

## Data

Tables (SQLite, created on open): `traffic_config` (per camera: UTC offset, regions, settings,
taught scenery lights), `traffic_images` (one row per still image: name, capture time, time source,
lighting, count, status, approval), `traffic_runs` and `traffic_objects` (each run and everything it
proposed), `traffic_detections` (the boxes; false alarms are kept with state `false_alarm`, replaced
runs as `superseded`), `traffic_feedback` (every correction with the image crop's hash),
`traffic_backgrounds`, `traffic_profiles` (each training result, adopted or rejected, with its
metrics and training set), `traffic_notifications`. Every change is also in the hash-chained audit log.

## API

All under `/api` (and `/api/v1`):

| Method | Path | |
|---|---|---|
| GET/POST | `/cases/{case}/traffic/cameras` | list / add a camera |
| GET/PUT | `/traffic/cameras/{camera}` | settings, UTC offset, regions |
| POST | `/traffic/cameras/{camera}/background` | learn background (waits) |
| POST | `/traffic/cameras/{camera}/train` | queue training, returns the job |
| GET | `/traffic/cameras/{camera}/profiles` | training history |
| GET | `/cases/{case}/traffic/images` | the image table (`camera_id`, `status` filters) |
| POST | `/cases/{case}/traffic/detect` | queue detection: `{"only": "pending" \| "unedited", "camera_id", "evidence_ids"}` |
| GET/PATCH | `/traffic/images/{evidence}` | one image with detections and run; set camera, time, lighting, own regions |
| POST | `/traffic/images/{evidence}/detect` | detect one image (`force` + `reason` to replace a reviewed result) |
| POST | `/traffic/images/{evidence}/detections` | add a missed vehicle `{"box": [x,y,w,h], "cls"}` |
| PATCH | `/traffic/detections/{id}` | move / resize / change class |
| POST | `/traffic/detections/{id}/false-alarm`, `/restore` | mark / unmark a false alarm |
| POST | `/traffic/images/{evidence}/approve` | `{"approved": true \| false}` |
| GET | `/traffic/images/{evidence}/overlay.png` | picture with boxes and regions |
| GET | `/cases/{case}/traffic/notifications`; POST `.../read` | training and batch notices |
| GET | `/cases/{case}/traffic/export.xlsx \| .csv \| .json`, `/dataset.zip` | exports |
| GET | `/cases/{case}/traffic/chart?q=…` | chart data from words |
| GET | `/cases/{case}/traffic/report.html \| .pdf?chart=…` | traffic report |

Corrections answer with `training_job` when they started automatic training.

## Validation status and limits

Tested (`backend/tests/test_traffic.py`) on synthetic night scenes with known lamps, a learned
street lamp and lit windows, and on one real daytime street frame from OpenCV's sample data (two
parked vehicles found, four pedestrians not counted). **Not yet measured on real traffic-camera
images**: detection rates by distance, lighting and weather are unknown until a labelled set from
the lab's own cameras is run. Reviewed images exported with *Training set* are that labelled set.

- The learned detector is trained on COCO photographs, not traffic-camera imagery. Small, distant,
  occluded, motion-blurred and unlit vehicles are missed more often.
- At night a vehicle is usually seen only by its lamps; two vehicles close together, or one with a
  lamp out, can be counted wrongly. Lamp pairs facing the camera at an angle may not pair.
- Scenery lights not yet learned (a newly lit window) can be counted as vehicles until learned.
- A vehicle parked in the same place in most images becomes part of the learned background, so the
  change member does not see it (the learned detector still can).
- Lighting rules can mistake a monochrome daytime camera for night_ir: override the image.
- Training adapts scoring and threshold only. Teaching the detector new appearances (for example a
  camera that sees vehicles only from far above) needs fine-tuning on the exported training set on
  a GPU machine and installing the resulting model as a lab model (`docs/ai-models.md`).
- Training metrics are measured on the examiner's own reviewed images; with few images they are
  unstable and optimistic. They are not a validation.

## Next

- Plates on the traffic page: run the existing plate reader (`forensic/intel/lpr.py`) on counted
  vehicles and compare with a wanted plate, with Arabic and Persian digits and plate formats.
- Mobile cameras: background learning assumes a fixed view; for dash and body cameras only the
  learned detector and the lamp member apply.
- Fine-tuning workflow: a documented script that trains YOLOX on the exported set and produces a
  model card for `python -m forensic.models install`.
