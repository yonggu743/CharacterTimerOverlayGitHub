"""Realtime screen capture utility.

Examples:
  python tools/screen_reader.py --list-monitors
  python tools/screen_reader.py --mode full --preview --fps 10
  python tools/screen_reader.py --mode region --region 100,100,800,600 --preview
  python tools/screen_reader.py --mode select --preview
  python tools/screen_reader.py --mode full --save-frame screen.png
"""

from __future__ import annotations

import argparse
import dataclasses
import time
from pathlib import Path
from typing import Generator, Iterable, Literal

import cv2
import mss
import numpy as np


@dataclasses.dataclass(frozen=True)
class CaptureRegion:
    left: int
    top: int
    width: int
    height: int

    @classmethod
    def parse(cls, value: str) -> "CaptureRegion":
        parts = [part.strip() for part in value.split(",")]
        if len(parts) != 4:
            raise argparse.ArgumentTypeError(
                "region must be left,top,width,height"
            )
        try:
            left, top, width, height = [int(part) for part in parts]
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                "region values must be integers"
            ) from exc
        if width <= 0 or height <= 0:
            raise argparse.ArgumentTypeError("region width/height must be > 0")
        return cls(left=left, top=top, width=width, height=height)

    def to_mss(self) -> dict[str, int]:
        return dataclasses.asdict(self)


class ScreenReader:
    """Captures the desktop as BGR numpy arrays suitable for OpenCV."""

    def __init__(
        self,
        monitor: int = 1,
        region: CaptureRegion | None = None,
        fps: float = 10.0,
    ) -> None:
        if fps <= 0:
            raise ValueError("fps must be > 0")
        self.monitor = monitor
        self.region = region
        self.frame_interval = 1.0 / fps
        self._sct = mss.MSS()
        self._capture_target = self._build_capture_target()

    @staticmethod
    def monitors() -> list[dict[str, int]]:
        with mss.MSS() as sct:
            return [dict(monitor) for monitor in sct.monitors]

    def close(self) -> None:
        self._sct.close()

    def __enter__(self) -> "ScreenReader":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def capture_target(self) -> dict[str, int]:
        return dict(self._capture_target)

    def grab(self) -> np.ndarray:
        """Return one frame in BGR channel order."""
        shot = self._sct.grab(self._capture_target)
        bgra = np.asarray(shot, dtype=np.uint8)
        return cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)

    def frames(self, max_frames: int | None = None) -> Generator[np.ndarray, None, None]:
        """Yield frames at the configured FPS."""
        count = 0
        next_frame_at = time.perf_counter()
        while max_frames is None or count < max_frames:
            now = time.perf_counter()
            if now < next_frame_at:
                time.sleep(next_frame_at - now)
            yield self.grab()
            count += 1
            next_frame_at += self.frame_interval

    def _build_capture_target(self) -> dict[str, int]:
        monitors = self._sct.monitors
        if self.region is not None:
            return self.region.to_mss()
        if self.monitor < 0 or self.monitor >= len(monitors):
            raise ValueError(
                f"monitor index {self.monitor} is invalid; "
                f"valid range is 0..{len(monitors) - 1}"
            )
        return dict(monitors[self.monitor])


CaptureMode = Literal["full", "region", "select"]


def print_monitors(monitors: Iterable[dict[str, int]]) -> None:
    for index, monitor in enumerate(monitors):
        print(
            f"{index}: left={monitor['left']} top={monitor['top']} "
            f"width={monitor['width']} height={monitor['height']}"
        )


def save_frame(reader: ScreenReader, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame = reader.grab()
    if not cv2.imwrite(str(output_path), frame):
        raise RuntimeError(f"failed to write {output_path}")
    print(f"saved {output_path}")


def select_region(monitor: int) -> CaptureRegion:
    with ScreenReader(monitor=monitor, fps=1.0) as reader:
        frame = reader.grab()
        monitor_info = reader.capture_target()

    window_name = "select capture region"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    rect = cv2.selectROI(window_name, frame, showCrosshair=True, fromCenter=False)
    cv2.destroyWindow(window_name)
    x, y, width, height = [int(value) for value in rect]
    if width <= 0 or height <= 0:
        raise RuntimeError("no region selected")
    region = CaptureRegion(
        left=monitor_info["left"] + x,
        top=monitor_info["top"] + y,
        width=width,
        height=height,
    )
    print(
        "selected region: "
        f"{region.left},{region.top},{region.width},{region.height}"
    )
    return region


def preview(reader: ScreenReader, max_frames: int | None) -> None:
    window_name = "screen-reader"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    try:
        for frame in reader.frames(max_frames=max_frames):
            cv2.imshow(window_name, frame)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
    finally:
        cv2.destroyWindow(window_name)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list-monitors",
        action="store_true",
        help="print available monitors and exit",
    )
    parser.add_argument(
        "--monitor",
        type=int,
        default=1,
        help="monitor index from --list-monitors; 0 is the virtual full desktop",
    )
    parser.add_argument(
        "--mode",
        choices=("full", "region", "select"),
        default="full",
        help=(
            "capture mode: full captures a monitor, region uses --region, "
            "select lets you choose a region with the mouse"
        ),
    )
    parser.add_argument(
        "--region",
        type=CaptureRegion.parse,
        help="capture rectangle for --mode region: left,top,width,height",
    )
    parser.add_argument("--fps", type=float, default=10.0, help="capture FPS")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="show realtime preview; press q or Esc to quit",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        help="stop after this many frames; useful for smoke tests",
    )
    parser.add_argument(
        "--save-frame",
        type=Path,
        help="save one frame to an image file and exit",
    )
    return parser.parse_args()


def resolve_region(mode: CaptureMode, monitor: int, region: CaptureRegion | None) -> CaptureRegion | None:
    if mode == "full":
        return None
    if mode == "region":
        if region is None:
            raise ValueError("--mode region requires --region left,top,width,height")
        return region
    if mode == "select":
        return select_region(monitor)
    raise ValueError(f"unsupported capture mode: {mode}")


def main() -> int:
    args = parse_args()
    if args.list_monitors:
        print_monitors(ScreenReader.monitors())
        return 0

    try:
        region = resolve_region(args.mode, args.monitor, args.region)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2

    reader = ScreenReader(monitor=args.monitor, region=region, fps=args.fps)
    try:
        if args.save_frame is not None:
            save_frame(reader, args.save_frame)
        if args.preview:
            preview(reader, args.max_frames)
        if args.save_frame is None and not args.preview:
            frame = reader.grab()
            print(f"captured frame: width={frame.shape[1]} height={frame.shape[0]}")
    finally:
        reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
