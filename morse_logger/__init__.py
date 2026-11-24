"""morse-logger - Morse (CW) logger for Linux/pipewire.

Pipeline: virtual audio cable (pipewire null sink) -> fldigi CW decoder
(headless, Xvfb) -> rx text stream -> timestamped logs (text + jsonl + sqlite).
"""
__version__ = "2.1.0"
APP_NAME = "morse-logger"
