# region imports
# Standard library imports
import os
import csv
import time
from datetime import datetime
os.environ["GST_PLUGIN_FEATURE_RANK"] = "vaapidecodebin:NONE"

# Third-party imports
import gi

gi.require_version("Gst", "1.0")
# Local application-specific imports
import hailo
from gi.repository import Gst

from hailo_apps.python.pipeline_apps.detection_simple.detection_simple_pipeline import (
    GStreamerDetectionSimpleApp,
)

from hailo_apps.python.core.common.hailo_logger import get_logger
from hailo_apps.python.core.gstreamer.gstreamer_app import app_callback_class

hailo_logger = get_logger(__name__)

# endregion imports

# User-defined class to be used in the callback function: Inheritance from the app_callback_class
class user_app_callback_class(app_callback_class):
    def __init__(self, csv_path="fps_log.csv"):
        super().__init__()
        self.last_time = time.time()
        self.fps_buffer = []  # ← store fps for 30 frames
        self.csv_file = open(csv_path, "w", newline="")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(["timestamp","frame_count", "fps"])  # header

    def __del__(self):
        if hasattr(self, 'csv_file') and self.csv_file:
            self.csv_file.close()

# User-defined callback function: This is the callback function that will be called when data is available from the pipeline
def app_callback(element, buffer, user_data):
    # Note: Frame counting is handled automatically by the framework wrapper
    frame_idx = user_data.get_count()
    hailo_logger.debug("Processing frame %s", frame_idx)
    # string_to_print = f"Frame count: {user_data.get_count()}\n"

    # FPS calculation and CSV logging
    now = time.time()
    fps = 1.0 / (now - user_data.last_time)
    user_data.last_time = now
    user_data.fps_buffer.append(fps)  # ← accumulate

    # Log average every 30 frames
    if frame_idx % 30 == 0 and len(user_data.fps_buffer) > 0:
        avg_fps = sum(user_data.fps_buffer) / len(user_data.fps_buffer)
        timestamp = datetime.now().isoformat()
        user_data.csv_writer.writerow([timestamp, frame_idx, f"{avg_fps:.2f}"])
        user_data.csv_file.flush()
        user_data.fps_buffer = []  # ← reset buffer after logging
        print(f"Frame count: {frame_idx} | Avg FPS (last 30): {avg_fps:.2f}")

    string_to_print = f"Frame count: {frame_idx}\nFPS: {fps:.2f}\n"

    if buffer is None:
        hailo_logger.warning("Received None buffer at frame=%s", user_data.get_count())
        return

    for detection in hailo.get_roi_from_buffer(buffer).get_objects_typed(
        hailo.HAILO_DETECTION
    ):
        string_to_print += (
            f"Detection: {detection.get_label()} Confidence: {detection.get_confidence():.2f}\n"
        )
    print(string_to_print)
    return


def main():
    hailo_logger.info("Starting Detection Simple App.")
    from hailo_apps.python.core.common.core import get_pipeline_parser
    parser = get_pipeline_parser()
    parser.add_argument(
        "--csv-path",
        default="fps_log.csv",
        help="Path to save FPS log CSV file (default: fps_log.csv)"
    )

    args, _ = parser.parse_known_args()
    user_data = user_app_callback_class(csv_path=args.csv_path)  # ← pass csv_path here
    app = GStreamerDetectionSimpleApp(app_callback, user_data, parser=parser)  # ← pass parser here
    app.run()


if __name__ == "__main__":
    main()
