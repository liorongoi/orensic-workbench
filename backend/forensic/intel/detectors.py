"""Object detector adapters.

Adapter contract: `detect(bgr_u8, classes, min_score) -> [{"cls", "score", "box": [x, y, w, h]}]` in input
pixels, plus `id`, `version` and the model's pinned SHA-256 for the record. The bundled adapter runs
YOLOX (Apache-2.0, COCO classes) through ONNX Runtime on verified weights.
"""

from __future__ import annotations

import cv2
import numpy as np

from .. import models

COCO = ("person bicycle car motorcycle airplane bus train truck boat traffic_light fire_hydrant stop_sign "
        "parking_meter bench bird cat dog horse sheep cow elephant bear zebra giraffe backpack umbrella handbag "
        "tie suitcase frisbee skis snowboard sports_ball kite baseball_bat baseball_glove skateboard surfboard "
        "tennis_racket bottle wine_glass cup fork knife spoon bowl banana apple sandwich orange broccoli carrot "
        "hot_dog pizza donut cake chair couch potted_plant bed dining_table toilet tv laptop mouse remote "
        "keyboard cell_phone microwave oven toaster sink refrigerator book clock vase scissors teddy_bear "
        "hair_drier toothbrush").split()
VEHICLES = ("bicycle", "car", "motorcycle", "bus", "truck")
DEFAULT_CLASSES = ("person", *VEHICLES, "backpack", "handbag", "suitcase", "umbrella", "knife", "cell_phone")


class YoloxDetector:
    version = "1.0.0"

    def __init__(self, model_id: str = "detect.yolox_tiny"):
        self.model = models.get(model_id)
        self.id = model_id
        self.size = int(self.model.get("input_size", 416))
        self.sha256 = self.model["sha256"]
        grids, strides = [], []
        for s in (8, 16, 32):
            n = self.size // s
            xv, yv = np.meshgrid(np.arange(n), np.arange(n))
            grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
            strides.append(np.full((n * n, 1), s))
        self._grid = np.concatenate(grids).astype(np.float32)
        self._stride = np.concatenate(strides).astype(np.float32)

    def describe(self) -> dict:
        return {"model": self.id, "sha256": self.sha256, "input_size": self.size, "adapter": f"yolox {self.version}",
                "licence": self.model["licence"], "device": models.device(self.id)}

    def detect(self, bgr: np.ndarray, classes: tuple[str, ...] | None = DEFAULT_CLASSES, min_score: float = 0.35,
               nms: float = 0.45) -> list[dict]:
        S = self.size
        h, w = bgr.shape[:2]
        r = min(S / h, S / w)
        pad = np.full((S, S, 3), 114, np.uint8)
        rs = cv2.resize(bgr, (int(round(w * r)), int(round(h * r))), interpolation=cv2.INTER_LINEAR)
        pad[: rs.shape[0], : rs.shape[1]] = rs
        x = pad.transpose(2, 0, 1)[None].astype(np.float32)
        out = models.run(self.id, x)[0].astype(np.float32)
        xy = (out[:, :2] + self._grid) * self._stride
        wh = np.exp(out[:, 2:4]) * self._stride
        scores = out[:, 4:5] * out[:, 5:]
        if classes:
            keep_cls = np.zeros(len(COCO), bool)
            keep_cls[[COCO.index(c) for c in classes if c in COCO]] = True
            scores = scores * keep_cls[None]
        cls = scores.argmax(1)
        sc = scores[np.arange(len(cls)), cls]
        k = sc >= min_score
        if not k.any():
            return []
        boxes = np.concatenate([xy[k] - wh[k] / 2, wh[k]], axis=1) / r
        sc, cls = sc[k], cls[k]
        res = []
        # Class-wise non-maximum suppression.
        for c in np.unique(cls):
            m = cls == c
            idx = cv2.dnn.NMSBoxes(boxes[m].tolist(), sc[m].tolist(), min_score, nms)
            for i in np.array(idx).reshape(-1):
                bx = boxes[m][i]
                x0, y0 = max(0.0, float(bx[0])), max(0.0, float(bx[1]))
                x1, y1 = min(float(w), float(bx[0] + bx[2])), min(float(h), float(bx[1] + bx[3]))
                if x1 - x0 < 2 or y1 - y0 < 2:
                    continue
                res.append({"cls": COCO[int(c)], "score": round(float(sc[m][i]), 4),
                            "box": [round(x0, 1), round(y0, 1), round(x1 - x0, 1), round(y1 - y0, 1)]})
        res.sort(key=lambda d: (-d["score"], d["box"][0]))
        return res
