#!/usr/bin/env python3
"""Minimal example: read a recorded D455 session as time-aligned frame sets.

    python read_session_example.py sessions/<session_id>           # print a summary
    python read_session_example.py sessions/<session_id> --show    # also show the frames (q quits)

Needs record_d455.py in the same folder (it contains the Session reader).
"""
import argparse

import numpy as np

from record_d455 import Session


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("session", help="session directory")
    parser.add_argument("--show", action="store_true", help="display color and depth with OpenCV")
    args = parser.parse_args()

    session = Session(args.session)
    device = session.manifest.get("device", {})
    print("Camera:", device.get("name"), "serial", device.get("serial_number"))
    print("Streams:", ", ".join(session.topics()))
    depth_stream = session.manifest["streams"]["depth"]
    print("Depth intrinsics:", depth_stream["intrinsics"])
    print("Depth scale: {:g} m per unit".format(session.depth_scale))

    count = 0
    for item in session.aligned_frames():
        # One item per depth frame. Other streams are the nearest frame in time, or None if there
        # is none within half a frame period. delta_ms tells how far apart they were captured.
        depth_m = item["depth"].astype(np.float32) * session.depth_scale  # metres, 0 = no measurement
        color = item.get("color")       # BGR uint8, or None
        infrared = item.get("ir_left")  # uint8, pixel-aligned with depth, or None
        if count % 30 == 0:
            valid = depth_m[depth_m > 0]
            print("t = {:.3f} s (epoch)  depth frame {}  median distance {:.2f} m  color dt {} ms".format(
                item["timestamp_ms"] / 1000.0, item["frame_number"],
                float(np.median(valid)) if valid.size else float("nan"),
                "{:+.2f}".format(item["delta_ms"]["color"]) if color is not None else "n/a"))
        if args.show:
            import cv2

            shown = cv2.applyColorMap(cv2.convertScaleAbs(depth_m, alpha=255.0 / 6.0), cv2.COLORMAP_JET)
            cv2.imshow("depth (0 to 6 m)", shown)
            if color is not None:
                cv2.imshow("color", color)
            if infrared is not None:
                cv2.imshow("infrared", infrared)
            if cv2.waitKey(1) & 0xFF in (27, ord("q")):
                break
        count += 1
    print("{} aligned frame sets".format(count))


if __name__ == "__main__":
    main()
