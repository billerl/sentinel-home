"""
Motion detection module for Sentinel Home.
"""

from .motion_detector import MotionDetector
from .network_discovery import NetworkCameraDiscovery, CameraEndpoint

__all__ = ["MotionDetector", "NetworkCameraDiscovery", "CameraEndpoint"]
