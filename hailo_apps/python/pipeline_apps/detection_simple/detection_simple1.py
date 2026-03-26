import os
import csv
import time
from datetime import datetime

os.environ["GST_PLUGIN_FEATURE_RANK"] = "vaapidecodebin:NONE"

import gi
gi.require_version("Gst", "1.0")

import hailo
from gi.repository import Gst

from hailo_apps.python.pipeline_apps.detection_simple.detection_simple_pipeline import (
    GStreamerDetectionSimpleApp,
)
from hailo_apps.python.core.common.hailo_logger import get_logger
from hailo_apps.python.core.gstreamer.gstreamer_app import app_callback_class

hailo_logger = get_logger(__name__)


class user_app_callback_class(app_callback_class):
    def __init__(self, csv_path="fps_log.csv"):
        super().__init__()
        self.last_time = time.time()

        self.time_buffer = []
        self.latency_buffer = []
        self.window_size = 30

        self.csv_file = open(csv_path, "w", newline="")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(["timestamp", "frame_count", "fps", "latency_ms"])

    def __del__(self):
        if self.csv_file:
            self.csv_file.close()


def app_callback(element, buffer, user_data):
    frame_idx = user_data.get_count()

    now = time.time()

    # latency (inter-frame)
    latency_ms = (now - user_data.last_time) * 1000
    user_data.last_time = now

    user_data.time_buffer.append(now)
    user_data.latency_buffer.append(latency_ms)

    if len(user_data.time_buffer) >= user_data.window_size:
        total_time = user_data.time_buffer[-1] - user_data.time_buffer[0]
        fps = (len(user_data.time_buffer) - 1) / total_time

        avg_latency = sum(user_data.latency_buffer) / len(user_data.latency_buffer)

        timestamp = datetime.now().isoformat()
        user_data.csv_writer.writerow(
            [timestamp, frame_idx, f"{fps:.2f}", f"{avg_latency:.2f}"]
        )
        user_data.csv_file.flush()

        print(f"Frame {frame_idx} | FPS: {fps:.2f} | Latency: {avg_latency:.2f} ms")

        user_data.time_buffer = []
        user_data.latency_buffer = []

    if buffer is None:
        return

    for detection in hailo.get_roi_from_buffer(buffer).get_objects_typed(
        hailo.HAILO_DETECTION
    ):
        print(
            f"Detection: {detection.get_label()} "
            f"Confidence: {detection.get_confidence():.2f}"
        )


def main():
    from hailo_apps.python.core.common.core import get_pipeline_parser

    parser = get_pipeline_parser()
    parser.add_argument("--csv-path", default="fps_log.csv")

    args, _ = parser.parse_known_args()

    user_data = user_app_callback_class(csv_path=args.csv_path)
    app = GStreamerDetectionSimpleApp(app_callback, user_data, parser=parser)
    app.run()


if __name__ == "__main__":
    main()