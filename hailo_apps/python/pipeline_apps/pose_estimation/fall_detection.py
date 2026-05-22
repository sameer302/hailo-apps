# region imports

import os
import math
import time
from collections import deque
import atexit

os.environ["GST_PLUGIN_FEATURE_RANK"] = "vaapidecodebin:NONE"

import gi
gi.require_version("Gst", "1.0")

import cv2
import hailo

from gi.repository import Gst

from hailo_apps.python.pipeline_apps.pose_estimation.pose_estimation_pipeline import (
    GStreamerPoseEstimationApp,
)
from hailo_apps.python.core.common.buffer_utils import (
    get_caps_from_pad,
    get_numpy_from_buffer,
)
from hailo_apps.python.core.common.hailo_logger import get_logger
from hailo_apps.python.core.gstreamer.gstreamer_app import app_callback_class

hailo_logger = get_logger(__name__)

# endregion imports


# -----------------------------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------------------------
FALL_ANGLE_THRESHOLD     = 45    # degrees — body angle below this = horizontal/fallen
CONFIDENCE_THRESHOLD     = 0.4   # ignore weak detections
VISIBILITY_THRESHOLD     = 0.2   # ignore low-visibility keypoints
HISTORY_WINDOW           = 10    # frames kept per track for temporal smoothing
FALL_CONFIRM_FRAMES      = 3     # consecutive horizontal frames before declaring FALL
RECOVERY_FRAMES          = 5     # consecutive upright frames to clear a fall
ASPECT_RATIO_THRESHOLD   = 1.2   # bbox width/height > this → likely horizontal body
VELOCITY_THRESHOLD       = 0.04  # normalised vertical drop per frame to flag rapid descent


# -----------------------------------------------------------------------------------------------
# User-defined class — holds per-track state
# -----------------------------------------------------------------------------------------------
class user_app_callback_class(app_callback_class):
    def __init__(self):
        super().__init__()
        # track_id → deque of (angle, bbox_aspect_ratio, nose_y_norm) tuples
        self.track_history: dict[int, deque] = {}
        # track_id → {"fall_count": int, "recover_count": int, "state": str}
        self.track_state: dict[int, dict] = {}
        self.frame_count = 0


# -----------------------------------------------------------------------------------------------
# Keypoint index map
# -----------------------------------------------------------------------------------------------
def get_keypoints() -> dict:
    return {
        "nose": 0,
        "left_eye": 1, "right_eye": 2,
        "left_ear": 3, "right_ear": 4,
        "left_shoulder": 5,  "right_shoulder": 6,
        "left_elbow": 7,     "right_elbow": 8,
        "left_wrist": 9,     "right_wrist": 10,
        "left_hip": 11,      "right_hip": 12,
        "left_knee": 13,     "right_knee": 14,
        "left_ankle": 15,    "right_ankle": 16,
    }


# -----------------------------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------------------------
def get_point(points, idx, bbox, width, height, vis_thresh=VISIBILITY_THRESHOLD):
    """
    Convert a normalised landmark point into pixel coordinates.
    Returns (px, py) or None when visibility is too low.
    """
    pt = points[idx]
    if hasattr(pt, "visibility") and pt.visibility() < vis_thresh:
        return None
    px = int((pt.x() * bbox.width() + bbox.xmin()) * width)
    py = int((pt.y() * bbox.height() + bbox.ymin()) * height)
    return (px, py)


def midpoint(a, b):
    return ((a[0] + b[0]) // 2, (a[1] + b[1]) // 2)


def calculate_body_angle(head: tuple, ankle: tuple) -> float:
    """
    Angle (degrees) of the head-to-ankle vector measured from vertical.
    0° = perfectly upright, 90° = lying flat.
    """
    dx = ankle[0] - head[0]
    dy = ankle[1] - head[1]   # positive = downward in image
    # atan2 from vertical: swap arguments so 0° = upright
    angle = math.degrees(math.atan2(abs(dx), abs(dy) + 1e-6))
    return angle


def smoothed_angle(history: deque) -> float:
    """Simple moving average of the angle stored in each history entry."""
    if not history:
        return 0.0
    return sum(h["angle"] for h in history) / len(history)


def detect_rapid_descent(history: deque) -> bool:
    """
    Returns True if the nose has dropped quickly over the last few frames —
    a classic signature of a trip-and-fall rather than deliberate lying down.
    """
    if len(history) < 2:
        return False
    oldest = history[0]["nose_y_norm"]
    newest = history[-1]["nose_y_norm"]
    frames  = len(history)
    velocity = (newest - oldest) / frames   # positive = moving downward
    return velocity > VELOCITY_THRESHOLD


def update_fall_state(state: dict, is_horizontal: bool) -> str:
    """
    Finite-state machine:
      NORMAL  → FALL_DETECTED  after FALL_CONFIRM_FRAMES consecutive horizontal frames
      FALL_DETECTED → NORMAL   after RECOVERY_FRAMES consecutive upright frames
    """
    if is_horizontal:
        state["fall_count"]    += 1
        state["recover_count"]  = 0
    else:
        state["recover_count"] += 1
        state["fall_count"]     = 0

    if state["state"] == "NORMAL":
        if state["fall_count"] >= FALL_CONFIRM_FRAMES:
            state["state"] = "FALL_DETECTED"
    elif state["state"] == "FALL_DETECTED":
        if state["recover_count"] >= RECOVERY_FRAMES:
            state["state"] = "NORMAL"

    return state["state"]


# -----------------------------------------------------------------------------------------------
# Callback
# -----------------------------------------------------------------------------------------------
def app_callback(element, buffer, user_data: user_app_callback_class):
    if buffer is None:
        hailo_logger.warning("Received None buffer.")
        return

    pad = element.get_static_pad("src")
    fmt, width, height = get_caps_from_pad(pad)

    frame = None
    if user_data.use_frame and fmt and width and height:
        frame = get_numpy_from_buffer(buffer, fmt, width, height)
        frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)   # convert ONCE, outside detection loop

    roi        = hailo.get_roi_from_buffer(buffer)
    detections = roi.get_objects_typed(hailo.HAILO_DETECTION)
    kp_dict    = get_keypoints()

    user_data.frame_count += 1

    for det_idx, detection in enumerate(detections):

        if detection.get_label() != "person":
            continue
        if detection.get_confidence() < CONFIDENCE_THRESHOLD:
            continue

        bbox = detection.get_bbox()

        # Use detection index as a simple track id when no tracker is attached
        track_id = getattr(detection, "get_id", lambda: det_idx)()

        # Initialise per-track bookkeeping
        if track_id not in user_data.track_history:
            user_data.track_history[track_id] = deque(maxlen=HISTORY_WINDOW)
            user_data.track_state[track_id]   = {
                "state": "NORMAL",
                "fall_count": 0,
                "recover_count": 0,
            }

        history = user_data.track_history[track_id]
        state   = user_data.track_state[track_id]

        landmarks = detection.get_objects_typed(hailo.HAILO_LANDMARKS)
        if not landmarks:
            continue

        points = landmarks[0].get_points()

        try:
            # ------------------------------------------------------------------
            # 1. Extract key body points (with visibility checks)
            # ------------------------------------------------------------------
            nose = get_point(points, kp_dict["nose"], bbox, width, height)

            l_shoulder = get_point(points, kp_dict["left_shoulder"],  bbox, width, height)
            r_shoulder = get_point(points, kp_dict["right_shoulder"], bbox, width, height)
            l_hip      = get_point(points, kp_dict["left_hip"],       bbox, width, height)
            r_hip      = get_point(points, kp_dict["right_hip"],      bbox, width, height)
            l_ankle    = get_point(points, kp_dict["left_ankle"],     bbox, width, height)
            r_ankle    = get_point(points, kp_dict["right_ankle"],    bbox, width, height)

            # Need at least nose + one ankle pair
            if nose is None:
                continue
            if l_ankle is None and r_ankle is None:
                continue

            # Midpoints (fall back to whichever side is visible)
            ankle = (
                midpoint(l_ankle, r_ankle) if (l_ankle and r_ankle)
                else l_ankle or r_ankle
            )

            # Optional: prefer shoulder-midpoint as the "top" when available
            if l_shoulder and r_shoulder:
                top_point = midpoint(l_shoulder, r_shoulder)
            else:
                top_point = nose

            # Hip midpoint for torso reference
            hip = (
                midpoint(l_hip, r_hip) if (l_hip and r_hip)
                else None
            )

            # ------------------------------------------------------------------
            # 2. Feature extraction
            # ------------------------------------------------------------------
            # Primary: angle of the body axis from vertical
            body_angle = calculate_body_angle(top_point, ankle)

            # Secondary: bounding-box aspect ratio  (wide box → likely lying)
            bbox_aspect = (bbox.width() * width) / (bbox.height() * height + 1e-6)

            # Normalised nose Y (0 = top of frame, 1 = bottom)
            nose_y_norm = nose[1] / height

            # Store in history for temporal reasoning
            history.append({
                "angle":       body_angle,
                "bbox_aspect": bbox_aspect,
                "nose_y_norm": nose_y_norm,
            })

            # ------------------------------------------------------------------
            # 3. Multi-cue fall decision (current frame)
            # ------------------------------------------------------------------
            avg_angle       = smoothed_angle(history)
            angle_cue       = avg_angle > FALL_ANGLE_THRESHOLD        # body not upright
            aspect_cue      = bbox_aspect > ASPECT_RATIO_THRESHOLD    # bbox wider than tall
            rapid_descent   = detect_rapid_descent(history)

            # A fall needs the angle cue PLUS at least one corroborating cue
            is_horizontal = angle_cue and (aspect_cue or rapid_descent)

            # ------------------------------------------------------------------
            # 4. Temporal confirmation (FSM)
            # ------------------------------------------------------------------
            current_state = update_fall_state(state, is_horizontal)
            fall_detected = (current_state == "FALL_DETECTED")

            # ------------------------------------------------------------------
            # 5. Draw on frame
            # ------------------------------------------------------------------
            if user_data.use_frame and frame is not None:

                # Bounding box pixel coords — clamped to frame edges
                x1 = max(0, int(bbox.xmin() * width))
                y1 = max(0, int(bbox.ymin() * height))
                x2 = min(width  - 1, int((bbox.xmin() + bbox.width())  * width))
                y2 = min(height - 1, int((bbox.ymin() + bbox.height()) * height))

                color      = (0, 0, 255) if fall_detected else (0, 200, 0)
                label_text = "FALL DETECTED" if fall_detected else "NORMAL"

                # ── Bounding box ──────────────────────────────────────────────
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)

                # ── Label: always drawn INSIDE the top of the bbox ───────────
                # This guarantees it is never clipped off-screen regardless of
                # where the person stands in the frame.
                font       = cv2.FONT_HERSHEY_SIMPLEX
                font_scale = 0.65
                thickness  = 2
                (tw, th), baseline = cv2.getTextSize(label_text, font, font_scale, thickness)

                pad    = 4
                rect_x1 = x1
                rect_y1 = y1
                rect_x2 = min(x1 + tw + pad * 2, x2)   # never wider than bbox
                rect_y2 = y1 + th + pad * 2

                # Filled colour rectangle behind text
                cv2.rectangle(frame, (rect_x1, rect_y1), (rect_x2, rect_y2), color, -1)

                # White text on top of the filled rect
                cv2.putText(
                    frame, label_text,
                    (rect_x1 + pad, rect_y1 + th + pad),
                    font, font_scale,
                    (255, 255, 255), thickness,
                )

                # ── Secondary info row (angle + aspect ratio) ─────────────────
                info_text = f"Ang:{avg_angle:.1f} AR:{bbox_aspect:.2f}"
                (iw, ih), _ = cv2.getTextSize(info_text, font, 0.45, 1)
                info_y1 = rect_y2
                info_y2 = info_y1 + ih + pad * 2
                cv2.rectangle(
                    frame,
                    (x1, info_y1),
                    (min(x1 + iw + pad * 2, x2), info_y2),
                    color, -1,
                )
                cv2.putText(
                    frame, info_text,
                    (x1 + pad, info_y1 + ih + pad),
                    font, 0.45,
                    (255, 255, 255), 1,
                )

                # ── Rapid-descent indicator ───────────────────────────────────
                if rapid_descent:
                    rd_y1 = info_y2
                    rd_text = "! RAPID DROP"
                    (rw, rh), _ = cv2.getTextSize(rd_text, font, 0.45, 1)
                    cv2.rectangle(
                        frame,
                        (x1, rd_y1),
                        (min(x1 + rw + pad * 2, x2), rd_y1 + rh + pad * 2),
                        (0, 100, 255), -1,
                    )
                    cv2.putText(
                        frame, rd_text,
                        (x1 + pad, rd_y1 + rh + pad),
                        font, 0.45,
                        (255, 255, 255), 1,
                    )

                # ── Body axis line ────────────────────────────────────────────
                cv2.line(frame, top_point, ankle, color, 2)

                # ── Keypoint dots ─────────────────────────────────────────────
                for pt in filter(None, [top_point, ankle, nose, hip]):
                    cv2.circle(frame, pt, 5, (255, 200, 0), -1)

                                # ------------------------------------------------------------------
                # Global status panel
                # ------------------------------------------------------------------

                panel_color = (0, 0, 180) if fall_detected else (0, 120, 0)

                cv2.rectangle(
                    frame,
                    (20, 20),
                    (320, 120),
                    panel_color,
                    -1
                )

                status_text = "FALL DETECTED" if fall_detected else "NORMAL"

                cv2.putText(
                    frame,
                    status_text,
                    (40, 70),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1.2,
                    (255, 255, 255),
                    3
                )

                cv2.putText(
                    frame,
                    f"Angle: {avg_angle:.1f}",
                    (40, 105),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (255, 255, 255),
                    2
                )

            hailo_logger.debug(
                f"Track={track_id} | Conf={detection.get_confidence():.2f} | "
                f"Angle={avg_angle:.1f} | AR={bbox_aspect:.2f} | "
                f"Descent={rapid_descent} | State={current_state}"
            )
            print(
                f"[{user_data.frame_count:05d}] "
                f"Track={track_id} Angle={avg_angle:.1f}° "
                f"AR={bbox_aspect:.2f} Descent={rapid_descent} → {current_state}"
            )

        except Exception as e:
            hailo_logger.error(f"Error processing pose for detection {det_idx}: {e}")

    if user_data.use_frame and frame is not None:
        # if not hasattr(user_data, "video_writer"):

        #     h, w = frame.shape[:2]

        #     fourcc = cv2.VideoWriter_fourcc(*'mp4v')

        #     user_data.video_writer = cv2.VideoWriter(
        #         "fall_detection_output.mp4",
        #         fourcc,
        #         20.0,
        #         (w, h)
        #     )
        # user_data.video_writer.write(frame)
        user_data.set_frame(frame)
        # Detect window close or 'q' press
        # key = cv2.waitKey(1) & 0xFF

        # if key == ord('q'):
        #     print("Exiting...")

        #     cv2.destroyAllWindows()

        #     os._exit(0)
        # ── Init writer on first frame (size known only at runtime) ──────────
        # if user_data.video_writer is None:
        #     h, w = frame.shape[:2]
        #     fourcc = cv2.VideoWriter_fourcc(*'XVID')
        #     user_data.video_writer = cv2.VideoWriter(
        #         "fall_detection_output.avi",
        #         fourcc,
        #         OUTPUT_FPS,
        #         (w, h),
        #     )
        #     hailo_logger.info(f"Video writer initialised → fall_detection_output.avi ({w}x{h})")

        # # ── Write annotated frame ─────────────────────────────────────────────
        # if user_data.video_writer.isOpened():
        #     user_data.video_writer.write(frame)


# -----------------------------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------------------------
def main():
    hailo_logger.info("Starting Fall Detection App.")
    user_data = user_app_callback_class()
    user_data.use_frame = True

    # Release writer cleanly on exit — prevents corrupted file
    # atexit.register(lambda: user_data.video_writer.release()
    #                 if user_data.video_writer else None)



    # fourcc = cv2.VideoWriter_fourcc(*'mp4v')

    # user_data.video_writer = cv2.VideoWriter(
    #     "fall_detection_output.mp4",
    #     fourcc,
    #     20.0,
    #     (1280, 720)
    # )

    # atexit.register(user_data.video_writer.release)

    # cv2.namedWindow("User Frame", cv2.WINDOW_NORMAL)
    # cv2.moveWindow("User Frame", 300, 100)
    app = GStreamerPoseEstimationApp(app_callback, user_data)

    app.run()


if __name__ == "__main__":
    main()