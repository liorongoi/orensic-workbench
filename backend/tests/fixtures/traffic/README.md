# Traffic test images

`street_day.jpg`: the first frame of `samples/data/vtest.avi` from the OpenCV repository
(https://github.com/opencv/opencv, distributed with OpenCV under its Apache-2.0 licence), saved as
JPEG quality 85. It shows a pedestrian street with two parked vehicles at the top right
(a car at about x 733-767, y 51-95 and a van/truck at about x 650-724, y 43-100). Used to check that
the learned detector finds vehicles and that pedestrians are not counted.

All other traffic test scenes are generated in the tests (synthetic night roads with lamps).
