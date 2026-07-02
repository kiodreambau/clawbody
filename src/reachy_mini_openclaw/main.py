"""ClawBody - Give your OpenClaw AI agent a physical robot body.

This module provides the main application that connects:
- OpenAI Realtime API for voice I/O (speech recognition + TTS)
- OpenClaw Gateway for AI intelligence (Clawson's brain)
- Reachy Mini robot for physical embodiment

Usage:
    # Console mode (direct audio)
    clawbody

    # With Gradio UI
    clawbody --gradio

    # With debug logging
    clawbody --debug
"""

import os
import sys
import time
import html
import json
import queue
import asyncio
import logging
import argparse
import threading
from pathlib import Path
from typing import Any, Optional
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

# Load environment from project root (override=True ensures .env takes precedence)
_project_root = Path(__file__).parent.parent.parent
load_dotenv(_project_root / ".env", override=True)

logger = logging.getLogger(__name__)


SUBSCRIPTION_UI_PORT = int(os.getenv("CLAWBODY_SUBSCRIPTION_PORT", "7861"))


class _SubscriptionRequestHandler(BaseHTTPRequestHandler):
    """Tiny local UI for OpenClaw subscription mode."""

    server: "_SubscriptionServer"

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("subscription-ui: " + format, *args)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/v1/mic":
            self._send_json({"muted": self.server.get_mic_muted()})
            return
        if path == "/api/v1/openclaw":
            self._send_json(self.server.openclaw_status())
            return
        if path not in ("/", "/chat"):
            self.send_error(404)
            return
        self._send_html(self._page())

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/v1/mic":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            data = json.loads(body) if body.strip() else {}
            muted = bool(data.get("muted"))
            self.server.set_mic_muted(muted)
            self._send_json({"muted": self.server.get_mic_muted()})
            return
        if path == "/api/v1/sound/test":
            self.server.play_feedback_sound()
            self._send_json({"played": True})
            return
        if path != "/chat":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        message = parse_qs(body).get("message", [""])[0].strip()
        if not message:
            self._send_html(self._page(error="Enter a message."))
            return
        try:
            response = self.server.submit_message(message)
            self._send_html(self._page(message=message, response=response))
        except Exception as e:
            self._send_html(self._page(message=message, error=str(e)))

    def _send_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def _page(
        message: str = "",
        response: str = "",
        error: str = "",
    ) -> str:
        message_html = html.escape(message)
        response_html = html.escape(response)
        error_html = html.escape(error)
        result = ""
        if response:
            result = f"<section><h2>OpenClaw</h2><p>{response_html}</p></section>"
        if error:
            result = f"<section class='error'><h2>Error</h2><p>{error_html}</p></section>"
        return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ClawBody Subscription</title>
  <style>
	    body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 32px; max-width: 760px; }}
	    textarea {{ box-sizing: border-box; width: 100%; min-height: 120px; font: inherit; padding: 12px; }}
	    button {{ margin-top: 12px; padding: 10px 16px; font: inherit; }}
	    .controls {{ display: flex; gap: 10px; flex-wrap: wrap; margin: 0 0 16px; }}
	    section {{ margin-top: 24px; padding: 16px; border: 1px solid #ddd; border-radius: 8px; }}
	    .status {{ background: #f7f7f8; color: #30343b; font-size: 14px; }}
	    .status strong {{ display: inline-block; min-width: 116px; }}
	    .error {{ border-color: #d33; color: #9b1111; }}
	  </style>
</head>
<body>
  <h1>ClawBody Subscription Mode</h1>
	  <div class="controls">
	    <button type="button" id="mic-toggle">Mic: loading</button>
	    <button type="button" id="sound-test">Play sound test</button>
	  </div>
	  <section class="status" id="openclaw-status">
	    <div><strong>OpenClaw</strong><span id="openclaw-connected">checking</span></div>
	    <div><strong>Session</strong><span id="openclaw-session">checking</span></div>
	    <div><strong>Last response</strong><span id="openclaw-last">none</span></div>
	  </section>
	  <form method="post" action="/chat">
    <textarea name="message" autofocus>{message_html}</textarea>
    <br>
    <button type="submit">Send to OpenClaw and speak</button>
  </form>
  {result}
  <script>
	    const micButton = document.getElementById("mic-toggle");
	    const soundButton = document.getElementById("sound-test");
	    const openclawConnected = document.getElementById("openclaw-connected");
	    const openclawSession = document.getElementById("openclaw-session");
	    const openclawLast = document.getElementById("openclaw-last");

	    async function refreshMic() {{
	      const res = await fetch("/api/v1/mic");
	      const state = await res.json();
	      micButton.dataset.muted = state.muted ? "true" : "false";
	      micButton.textContent = state.muted ? "Mic: paused" : "Mic: listening";
	    }}

	    async function refreshOpenClaw() {{
	      const res = await fetch("/api/v1/openclaw");
	      const state = await res.json();
	      openclawConnected.textContent = state.connected ? "connected" : "not connected";
	      openclawSession.textContent = state.fullSessionKey || "unavailable";
	      if (state.lastError) {{
	        openclawLast.textContent = "error: " + state.lastError;
	      }} else if (state.lastResponseSeconds) {{
	        openclawLast.textContent = state.lastResponseSeconds + "s";
	      }} else {{
	        openclawLast.textContent = "none yet";
	      }}
	    }}

    micButton.addEventListener("click", async () => {{
      const muted = micButton.dataset.muted !== "true";
      await fetch("/api/v1/mic", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ muted }}),
      }});
      await refreshMic();
    }});

    soundButton.addEventListener("click", async () => {{
      await fetch("/api/v1/sound/test", {{ method: "POST" }});
    }});

	    refreshMic().catch(() => {{
	      micButton.textContent = "Mic: unavailable";
	    }});
	    refreshOpenClaw().catch(() => {{
	      openclawConnected.textContent = "unavailable";
	    }});
	    setInterval(() => {{
	      refreshOpenClaw().catch(() => {{}});
	    }}, 5000);
	  </script>
</body>
</html>"""


class _SubscriptionServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], app: "ClawBodyCore") -> None:
        super().__init__(address, _SubscriptionRequestHandler)
        self.app = app

    def submit_message(self, message: str) -> str:
        future = asyncio.run_coroutine_threadsafe(
            self.app.handle_subscription_message(message),
            self.app.loop,
        )
        timeout = 60.0
        if self.app.openclaw_bridge is not None:
            timeout = self.app.openclaw_bridge.timeout + 15.0
        return future.result(timeout=timeout)

    def get_mic_muted(self) -> bool:
        return self.app.get_mic_muted()

    def set_mic_muted(self, muted: bool) -> None:
        self.app.set_mic_muted(muted)

    def play_feedback_sound(self) -> None:
        self.app.play_feedback_sound()

    def openclaw_status(self) -> dict[str, Any]:
        return self.app.get_openclaw_status()


def setup_logging(debug: bool = False) -> None:
    """Configure logging for the application.
    
    Args:
        debug: Enable debug level logging
    """
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    
    # Reduce noise from libraries
    if not debug:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("websockets").setLevel(logging.WARNING)
        logging.getLogger("openai").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments.
    
    Returns:
        Parsed arguments namespace
    """
    parser = argparse.ArgumentParser(
        description="ClawBody - Give your OpenClaw AI agent a physical robot body",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Run in console mode
    clawbody

    # Run with Gradio web UI
    clawbody --gradio

    # Connect to specific robot
    clawbody --robot-name my-reachy

    # Use different OpenClaw gateway
    clawbody --gateway-url http://192.168.1.100:18790
        """
    )
    
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )
    parser.add_argument(
        "--gradio",
        action="store_true",
        help="Launch Gradio web UI instead of console mode"
    )
    parser.add_argument(
        "--robot-name",
        type=str,
        help="Robot name for connection (default: auto-discover)"
    )
    parser.add_argument(
        "--gateway-url",
        type=str,
        default=os.getenv("OPENCLAW_GATEWAY_URL", "ws://localhost:18789"),
        help="OpenClaw gateway URL (from OPENCLAW_GATEWAY_URL env or default)"
    )
    parser.add_argument(
        "--no-camera",
        action="store_true",
        help="Disable camera functionality"
    )
    parser.add_argument(
        "--no-openclaw",
        action="store_true",
        help="Disable OpenClaw integration"
    )
    parser.add_argument(
        "--no-face-tracking",
        action="store_true",
        help="Disable face tracking"
    )
    parser.add_argument(
        "--local-vision",
        action="store_true",
        help="Enable local vision processing with SmolVLM2"
    )
    parser.add_argument(
        "--profile",
        type=str,
        help="Custom personality profile to use"
    )
    
    return parser.parse_args()


class ClawBodyCore:
    """ClawBody core application controller.
    
    This class orchestrates all components:
    - Reachy Mini robot connection and movement control
    - OpenAI Realtime API for voice I/O
    - OpenClaw gateway bridge for AI intelligence
    - Audio input/output loops
    """
    
    def __init__(
        self,
        gateway_url: str = "ws://localhost:18789",
        robot_name: Optional[str] = None,
        enable_camera: bool = True,
        enable_openclaw: bool = True,
        robot: Optional["ReachyMini"] = None,
        external_stop_event: Optional[threading.Event] = None,
    ):
        """Initialize the application.
        
        Args:
            gateway_url: OpenClaw gateway URL
            robot_name: Optional robot name for connection
            enable_camera: Whether to enable camera functionality
            enable_openclaw: Whether to enable OpenClaw integration
            robot: Optional pre-initialized robot (for app framework)
            external_stop_event: Optional external stop event
        """
        from reachy_mini import ReachyMini
        from reachy_mini_openclaw.config import config
        from reachy_mini_openclaw.moves import MovementManager
        from reachy_mini_openclaw.audio.head_wobbler import HeadWobbler
        from reachy_mini_openclaw.openclaw_bridge import OpenClawBridge
        from reachy_mini_openclaw.tools.core_tools import ToolDependencies
        from reachy_mini_openclaw.openai_realtime import OpenAIRealtimeHandler
        
        self.gateway_url = gateway_url
        self._external_stop_event = external_stop_event
        self._owns_robot = robot is None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._subscription_server: Optional[_SubscriptionServer] = None
        self._subscription_server_thread: Optional[threading.Thread] = None
        self._subscription_voice_queue: Optional[queue.Queue] = None
        self._subscription_stt_pipeline: Any = None
        self._subscription_voice_enabled = bool(enable_openclaw and not config.OPENAI_API_KEY)
        self._mic_muted = False
        
        # Validate configuration
        errors = config.validate()
        if errors:
            for error in errors:
                logger.error("Config error: %s", error)
            sys.exit(1)
        
        # Connect to robot
        if robot is not None:
            self.robot = robot
            logger.info("Using provided Reachy Mini instance")
        else:
            logger.info("Connecting to Reachy Mini...")
            robot_kwargs = {}
            if robot_name:
                robot_kwargs["robot_name"] = robot_name
                
            try:
                self.robot = ReachyMini(**robot_kwargs)
            except TimeoutError as e:
                logger.error("Connection timeout: %s", e)
                logger.error("Check that the robot is powered on and reachable.")
                sys.exit(1)
            except Exception as e:
                logger.error("Robot connection failed: %s", e)
                sys.exit(1)
                
            logger.info("Connected to robot: %s", self.robot.client.get_status())
        
        # Initialize movement system
        logger.info("Initializing movement system...")
        self.movement_manager = MovementManager(current_robot=self.robot)
        self.head_wobbler = HeadWobbler(
            set_speech_offsets=self.movement_manager.set_speech_offsets
        )
        
        # Initialize OpenClaw bridge
        self.openclaw_bridge = None
        if enable_openclaw:
            logger.info("Initializing OpenClaw bridge...")
            self.openclaw_bridge = OpenClawBridge(
                gateway_url=gateway_url,
                gateway_token=config.OPENCLAW_TOKEN,
            )
        
        # Camera worker for video streaming and frame capture
        self.camera_worker = None
        self.head_tracker = None
        self.vision_manager = None
        
        if enable_camera:
            logger.info("Initializing camera worker...")
            from reachy_mini_openclaw.camera_worker import CameraWorker
            
            # Initialize head tracker for local face tracking
            if config.ENABLE_FACE_TRACKING:
                self.head_tracker = self._initialize_head_tracker(config.HEAD_TRACKER_TYPE)
            
            # Initialize camera worker with head tracker
            self.camera_worker = CameraWorker(
                reachy_mini=self.robot,
                head_tracker=self.head_tracker,
            )
            
            # Enable/disable head tracking based on whether we have a tracker
            self.camera_worker.set_head_tracking_enabled(self.head_tracker is not None)
            
            # Initialize local vision processor if enabled
            if config.ENABLE_LOCAL_VISION:
                self.vision_manager = self._initialize_vision_manager()
        
        # Create tool dependencies
        self.deps = ToolDependencies(
            movement_manager=self.movement_manager,
            head_wobbler=self.head_wobbler,
            robot=self.robot,
            camera_worker=self.camera_worker,
            openclaw_bridge=self.openclaw_bridge,
            vision_manager=self.vision_manager,
        )
        
        # Initialize OpenAI Realtime handler with OpenClaw bridge
        self.handler = OpenAIRealtimeHandler(
            deps=self.deps,
            openclaw_bridge=self.openclaw_bridge,
        )
        
        # State
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    def _start_subscription_ui(self) -> None:
        """Start local text UI for subscription-backed OpenClaw chat."""
        if self._subscription_server is not None:
            return
        try:
            self._subscription_server = _SubscriptionServer(
                ("127.0.0.1", SUBSCRIPTION_UI_PORT),
                self,
            )
        except OSError as e:
            logger.warning("Subscription UI not started on port %d: %s", SUBSCRIPTION_UI_PORT, e)
            return

        self._subscription_server_thread = threading.Thread(
            target=self._subscription_server.serve_forever,
            name="clawbody-subscription-ui",
            daemon=True,
        )
        self._subscription_server_thread.start()
        logger.info(
            "Subscription UI ready at http://127.0.0.1:%d",
            SUBSCRIPTION_UI_PORT,
        )

    def _stop_subscription_ui(self) -> None:
        if self._subscription_server is None:
            return
        try:
            self._subscription_server.shutdown()
            self._subscription_server.server_close()
        except Exception as e:
            logger.debug("Subscription UI shutdown: %s", e)
        self._subscription_server = None
        self._subscription_server_thread = None

    def _start_subscription_voice(self) -> None:
        """Start local STT for subscription mode."""
        if not self._subscription_voice_enabled or self._subscription_stt_pipeline is not None:
            return
        try:
            from reachy_mini_reactions.speech_to_text.pipeline import SpeechToTextPipeline

            self._subscription_voice_queue = queue.Queue()
            model = os.getenv("CLAWBODY_STT_MODEL", "tiny")
            self._subscription_stt_pipeline = SpeechToTextPipeline(
                model=model,
                event_queue=self._subscription_voice_queue,
                echo=False,
                reachy_mini=self.robot,
            )
            self._subscription_stt_pipeline.start()
            logger.info("Subscription voice input ready using Moonshine %s", model)
        except Exception as e:
            self._subscription_stt_pipeline = None
            self._subscription_voice_queue = None
            logger.warning("Subscription voice input not available: %s", e)

    def _stop_subscription_voice(self) -> None:
        if self._subscription_stt_pipeline is None:
            return
        try:
            self._subscription_stt_pipeline.stop()
        except Exception as e:
            logger.debug("Subscription voice stop: %s", e)
        self._subscription_stt_pipeline = None
        self._subscription_voice_queue = None

    def get_mic_muted(self) -> bool:
        return self._mic_muted

    def set_mic_muted(self, muted: bool) -> None:
        self._mic_muted = muted
        logger.info("Mic %s", "paused" if muted else "listening")

    def get_openclaw_status(self) -> dict[str, Any]:
        if self.openclaw_bridge is None:
            return {
                "connected": False,
                "lastError": "OpenClaw bridge is not initialized",
            }
        return self.openclaw_bridge.status()

    def play_feedback_sound(self) -> None:
        """Play a short cue through Reachy's speaker path."""
        sound_path = Path(
            os.getenv(
                "CLAWBODY_FEEDBACK_SOUND",
                str(Path.home() / ".openclaw/workspace/assets/sounds/soft-click-candidate.mp3"),
            )
        )
        try:
            if sound_path.exists():
                self.robot.media.play_sound(str(sound_path))
                return

            import numpy as np

            sample_rate = self.robot.media.get_output_audio_samplerate()
            duration = 0.14
            t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
            envelope = np.linspace(1.0, 0.0, t.size, dtype=np.float32)
            tone = (np.sin(2 * np.pi * 660 * t) * envelope * 0.12).astype("float32")
            self.robot.media.push_audio_sample(tone)
        except Exception as e:
            logger.warning("Feedback sound failed: %s", e)

    async def subscription_voice_loop(self) -> None:
        """Forward local STT results to OpenClaw subscription backend."""
        if self._subscription_voice_queue is None:
            return
        while not self._should_stop():
            try:
                event = await asyncio.to_thread(self._subscription_voice_queue.get, True, 0.5)
            except queue.Empty:
                continue
            if event.get("type") != "stt_result":
                continue
            text = str(event.get("text") or "").strip()
            if not text:
                continue
            if self._mic_muted:
                logger.info("Subscription voice ignored while mic is paused: %s", text)
                continue
            logger.info("Subscription voice user: %s", text)
            try:
                await self.handle_subscription_message(text)
            except Exception as e:
                logger.warning("Subscription voice request failed: %s", e)

    async def handle_subscription_message(self, message: str) -> str:
        """Send text to OpenClaw and speak the response locally."""
        if self.openclaw_bridge is None:
            raise RuntimeError("OpenClaw bridge is not initialized")
        if not self.openclaw_bridge.is_connected:
            connected = await self.openclaw_bridge.connect()
            if not connected:
                raise RuntimeError("OpenClaw gateway is not connected")

        response = await self.openclaw_bridge.chat(
            message,
            system_context=(
                "User is speaking through the Reachy Mini clawbody subscription UI. "
                "Keep the response concise for spoken output."
            ),
        )
        if response.error:
            raise RuntimeError(response.error)

        text = response.content.strip()
        if text and hasattr(self.handler, "speak_text"):
            await self.handler.speak_text(text)
        return text
        
    def _initialize_vision_manager(self) -> Optional[Any]:
        """Initialize local vision processor (SmolVLM2).
        
        Returns:
            VisionManager instance or None if initialization fails
        """
        if self.camera_worker is None:
            logger.warning("Cannot initialize vision manager without camera worker")
            return None
        
        try:
            from reachy_mini_openclaw.vision.processors import (
                VisionConfig, 
                initialize_vision_manager,
            )
            from reachy_mini_openclaw.config import config
            
            vision_config = VisionConfig(
                model_path=config.LOCAL_VISION_MODEL,
                device_preference=config.VISION_DEVICE,
                hf_home=config.HF_HOME,
            )
            
            logger.info("Initializing local vision processor (SmolVLM2)...")
            vision_manager = initialize_vision_manager(self.camera_worker, vision_config)
            
            if vision_manager is not None:
                logger.info("Local vision processor initialized")
            else:
                logger.warning("Local vision processor failed to initialize")
            
            return vision_manager
            
        except ImportError as e:
            logger.warning(f"Local vision not available: {e}")
            logger.warning("Install with: pip install torch transformers")
            return None
        except Exception as e:
            logger.error(f"Failed to initialize vision manager: {e}")
            return None
    
    def _initialize_head_tracker(self, tracker_type: Optional[str] = None) -> Optional[Any]:
        """Initialize head tracker for local face tracking.
        
        Args:
            tracker_type: Type of tracker ("yolo", "mediapipe", or None for auto)
            
        Returns:
            Initialized head tracker or None if initialization fails
        """
        # Default to YOLO if not specified
        if tracker_type is None:
            tracker_type = "yolo"
        
        if tracker_type == "yolo":
            try:
                from reachy_mini_openclaw.vision.yolo_head_tracker import HeadTracker
                logger.info("Initializing YOLO face tracker...")
                tracker = HeadTracker(device="cpu")  # CPU is fast enough for face detection
                logger.info("YOLO face tracker initialized")
                return tracker
            except ImportError as e:
                logger.warning(f"YOLO tracker not available: {e}")
                logger.warning("Install with: pip install ultralytics supervision")
            except Exception as e:
                logger.error(f"Failed to initialize YOLO tracker: {e}")
        
        elif tracker_type == "mediapipe":
            try:
                from reachy_mini_openclaw.vision.mediapipe_tracker import HeadTracker
                logger.info("Initializing MediaPipe face tracker...")
                tracker = HeadTracker()
                logger.info("MediaPipe face tracker initialized")
                return tracker
            except ImportError as e:
                logger.warning(f"MediaPipe tracker not available: {e}")
            except Exception as e:
                logger.error(f"Failed to initialize MediaPipe tracker: {e}")
        
        logger.warning("No face tracker available - face tracking disabled")
        return None
        
    def _should_stop(self) -> bool:
        """Check if we should stop."""
        if self._stop_event.is_set():
            return True
        if self._external_stop_event is not None and self._external_stop_event.is_set():
            return True
        return False
        
    async def record_loop(self) -> None:
        """Read audio from robot microphone and send to handler."""
        input_sr = self.robot.media.get_input_audio_samplerate()
        logger.info("Recording at %d Hz", input_sr)
        
        while not self._should_stop():
            audio_frame = self.robot.media.get_audio_sample()
            if audio_frame is not None and not self._mic_muted:
                await self.handler.receive((input_sr, audio_frame))
            await asyncio.sleep(0.01)
            
    async def play_loop(self) -> None:
        """Play audio from handler through robot speakers."""
        output_sr = self.robot.media.get_output_audio_samplerate()
        logger.info("Playing at %d Hz", output_sr)
        
        while not self._should_stop():
            output = await self.handler.emit()
            if output is not None:
                if isinstance(output, tuple):
                    input_sr, audio_data = output
                    
                    # Convert to float32 and normalize (OpenAI sends int16)
                    audio_data = audio_data.flatten().astype("float32") / 32768.0
                    
                    # Reduce volume to prevent distortion (0.5 = 50% volume)
                    audio_data = audio_data * 0.5
                    
                    # Resample if needed
                    if input_sr != output_sr:
                        from scipy.signal import resample
                        num_samples = int(len(audio_data) * output_sr / input_sr)
                        audio_data = resample(audio_data, num_samples).astype("float32")
                        
                    self.robot.media.push_audio_sample(audio_data)
                # else: it's an AdditionalOutputs (transcript) - handle in UI mode
                
            await asyncio.sleep(0.01)
            
    async def run(self) -> None:
        """Run the main application loop."""
        self.loop = asyncio.get_running_loop()
        self._start_subscription_ui()

        # Test OpenClaw connection
        if self.openclaw_bridge is not None:
            connected = await self.openclaw_bridge.connect()
            if connected:
                logger.info("OpenClaw gateway connected")
            else:
                logger.warning("OpenClaw gateway not available - some features disabled")
        
        # Enable motors and move to neutral pose
        logger.info("Enabling motors and moving to neutral position...")
        try:
            self.robot.enable_motors()
            from reachy_mini.utils import create_head_pose
            neutral = create_head_pose(0, 0, 0, 0, 0, 0, degrees=True)
            self.robot.goto_target(
                head=neutral,
                antennas=[0.0, 0.0],
                duration=2.0,
                body_yaw=0.0,
            )
            time.sleep(2)  # Wait for goto to complete
            logger.info("Robot at neutral position with motors enabled")
        except Exception as e:
            logger.error("Failed to initialize robot pose: %s", e)
        
        # Wire up camera worker to movement manager for face tracking
        if self.camera_worker is not None:
            self.movement_manager.camera_worker = self.camera_worker
            logger.info("Face tracking connected to movement system")
        
        # Start movement system
        logger.info("Starting movement system...")
        self.movement_manager.start()
        self.head_wobbler.start()
        
        # Start camera worker for video streaming
        if self.camera_worker is not None:
            logger.info("Starting camera worker...")
            self.camera_worker.start()
        
        # Start local vision processor if available
        if self.vision_manager is not None:
            logger.info("Starting local vision processor...")
            self.vision_manager.start()
        
        # Start audio
        logger.info("Starting audio...")
        if not self._subscription_voice_enabled:
            self.robot.media.start_recording()
        self.robot.media.start_playing()
        time.sleep(1)  # Let pipelines initialize

        self._start_subscription_voice()
        
        logger.info("Ready! Speak to me...")
        
        # Start OpenAI handler in background. Without OPENAI_API_KEY this runs
        # the OpenClaw subscription mode instead of Realtime API.
        handler_task = asyncio.create_task(self.handler.start_up(), name="openai-handler")
        
        # Start audio loops
        self._tasks = [
            handler_task,
            asyncio.create_task(self.play_loop(), name="play-loop"),
        ]
        if self._subscription_voice_enabled:
            self._tasks.append(
                asyncio.create_task(
                    self.subscription_voice_loop(),
                    name="subscription-voice-loop",
                )
            )
        else:
            self._tasks.append(asyncio.create_task(self.record_loop(), name="record-loop"))
        
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            logger.info("Tasks cancelled")
            
    def stop(self) -> None:
        """Stop everything."""
        logger.info("Stopping...")
        self._stop_event.set()
        
        # Cancel tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()

        self._stop_subscription_voice()
        self._stop_subscription_ui()
                
        # Stop movement system
        self.head_wobbler.stop()
        self.movement_manager.stop()
        
        # Stop vision manager
        if self.vision_manager is not None:
            self.vision_manager.stop()
        
        # Stop camera worker
        if self.camera_worker is not None:
            self.camera_worker.stop()
        
        # Disconnect OpenClaw bridge
        if self.openclaw_bridge is not None:
            try:
                asyncio.get_event_loop().run_until_complete(
                    self.openclaw_bridge.disconnect()
                )
            except Exception as e:
                logger.debug("OpenClaw disconnect: %s", e)
        
        # Close resources if we own them
        if self._owns_robot:
            try:
                self.robot.media.close()
            except Exception as e:
                logger.debug("Media close: %s", e)
            self.robot.client.disconnect()
            
        logger.info("Stopped")


class ClawBodyApp:
    """ClawBody - Reachy Mini Apps entry point.
    
    This class allows ClawBody to be installed and run from
    the Reachy Mini dashboard as a Reachy Mini App.
    """
    
    custom_app_url: Optional[str] = f"http://127.0.0.1:{SUBSCRIPTION_UI_PORT}"
    
    def run(self, reachy_mini, stop_event: threading.Event) -> None:
        """Run ClawBody as a Reachy Mini App.
        
        Args:
            reachy_mini: Pre-initialized ReachyMini instance
            stop_event: Threading event to signal stop
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        gateway_url = os.getenv("OPENCLAW_GATEWAY_URL", "ws://localhost:18789")
        
        app = ClawBodyCore(
            gateway_url=gateway_url,
            robot=reachy_mini,
            external_stop_event=stop_event,
        )
        
        try:
            loop.run_until_complete(app.run())
        except Exception as e:
            logger.error("Error running app: %s", e)
        finally:
            app.stop()
            loop.close()


def main() -> None:
    """Main entry point."""
    args = parse_args()
    setup_logging(args.debug)
    
    # Set custom profile if specified
    if args.profile:
        from reachy_mini_openclaw.config import set_custom_profile
        set_custom_profile(args.profile)
    
    # Configure face tracking and local vision from args
    from reachy_mini_openclaw.config import (
        set_face_tracking_enabled, 
        set_local_vision_enabled,
    )
    if args.no_face_tracking:
        set_face_tracking_enabled(False)
    if args.local_vision:
        set_local_vision_enabled(True)
    
    if args.gradio:
        # Launch Gradio UI
        logger.info("Starting Gradio UI...")
        from reachy_mini_openclaw.gradio_app import launch_gradio
        launch_gradio(
            gateway_url=args.gateway_url,
            robot_name=args.robot_name,
            enable_camera=not args.no_camera,
            enable_openclaw=not args.no_openclaw,
        )
    else:
        # Console mode
        app = ClawBodyCore(
            gateway_url=args.gateway_url,
            robot_name=args.robot_name,
            enable_camera=not args.no_camera,
            enable_openclaw=not args.no_openclaw,
        )
        
        try:
            asyncio.run(app.run())
        except KeyboardInterrupt:
            logger.info("Interrupted")
        finally:
            app.stop()


if __name__ == "__main__":
    main()
