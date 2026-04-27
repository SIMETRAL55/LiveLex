"""
LiveRex — Real-time meeting transcription for Ubuntu.

Entry point.  Parses arguments, loads config, performs first-run checks,
wires the full pipeline, and runs until Ctrl+C.

Pipeline data flow:
  AudioCapture
    → audio_queue (asyncio.Queue, float32 16kHz mono chunks)
    → VADProcessor.process()
        → on_chunk  → streaming_queue
        → on_utterance_end → streaming.notify_utterance_end()
    → StreamingTranscriber.run(streaming_queue, on_text, on_newline)
        → on_text     → overlay.append_text()
        → on_newline  → overlay.append_newline()
  CaptionOverlay (PyQt6 thread, always-on-top floating window)
  GlobalHotKeys (pynput, Ctrl+Shift+H → toggle visibility)

Usage:
    python main.py
    python main.py --backend chirp2 --language ja --debug
    python main.py --config /path/to/config.yaml
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import]

from utils.logger import setup_logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# First-run checks
# ---------------------------------------------------------------------------

def _check_audio_device() -> None:
    """
    Verify that at least one PulseAudio/PipeWire monitor source is available.
    """
    from utils import audio_utils

    monitors = audio_utils.list_monitor_sources()
    if not monitors:
        print(
            "ERROR: No PulseAudio/PipeWire monitor source found.\n\n"
            "LiveRex captures system audio via a monitor source.  Ensure:\n"
            "  1. PipeWire or PulseAudio is running\n"
            "  2. An audio output device is active\n\n"
            "Check available sources:\n"
            "  pactl list sources short\n",
            file=sys.stderr,
        )
        raise SystemExit(1)

    logger.info("Audio check OK — found %d monitor source(s):", len(monitors))
    for m in monitors:
        logger.info("  [%d] %s", m.get("index", -1), m["name"])


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class Pipeline:
    """
    The complete LiveRex pipeline wired together.
    """

    def __init__(
        self,
        config: dict[str, Any],
        backend: str,
        transcript_writer: Any = None,
    ) -> None:
        from audio.capture import AudioCapture
        from audio.vad import VADProcessor
        from transcription.streaming import StreamingTranscriber
        from ui.overlay import CaptionOverlay

        cfg_audio = config["audio"]
        cfg_vad = config["vad"]

        # ── Overlay ───────────────────────────────────────────────────
        self._overlay = CaptionOverlay(config["overlay"])

        # ── Audio capture ─────────────────────────────────────────────
        device = cfg_audio["device"]
        self._capture = AudioCapture(
            device=None if device == "auto" else device,
            sample_rate=int(cfg_audio["sample_rate"]),
            chunk_ms=int(cfg_audio["chunk_ms"]),
        )

        # ── VAD ───────────────────────────────────────────────────────
        self._vad = VADProcessor(
            sample_rate=int(cfg_audio["sample_rate"]),
            threshold=float(cfg_vad["threshold"]),
            min_silence_ms=int(cfg_vad["min_silence_ms"]),
            min_speech_ms=int(cfg_vad["min_speech_ms"]),
            buffer_max_seconds=int(cfg_audio["buffer_max_seconds"]),
        )

        # ── Transcription backend ─────────────────────────────────────
        logger.info("Loading transcription backend: %s", backend)
        if backend == "chirp2":
            from transcription.chirp2 import Chirp2Transcriber
            self._transcriber = Chirp2Transcriber(config["chirp2"])
        else:
            from transcription.local import LocalTranscriber
            self._transcriber = LocalTranscriber(config["local"])

        self._transcript_writer = transcript_writer

        # ── Streaming transcriber ─────────────────────────────────────
        self._streaming = StreamingTranscriber(self._transcriber, config, transcript_writer)

        self._hotkey_listener = None

        self._overlay_visible: bool = True
        self._loop: asyncio.AbstractEventLoop | None = None

        logger.info("Pipeline initialised (backend=%s)", backend)

    async def run(self) -> None:
        audio_queue: asyncio.Queue = asyncio.Queue()
        streaming_queue: asyncio.Queue = asyncio.Queue()
        self._loop = asyncio.get_event_loop()

        async def on_chunk(chunk) -> None:
            await streaming_queue.put(chunk)

        async def on_utterance_end() -> None:
            self._streaming.notify_utterance_end()

        async def on_text(text: str) -> None:
            self._overlay.append_text(text)

        async def on_interim(text: str) -> None:
            self._overlay.set_interim_text(text)

        async def on_newline() -> None:
            self._overlay.append_newline()

        self._start_hotkey_listener()

        logger.info("Starting audio capture…")
        await self._capture.start(audio_queue)

        try:
            await asyncio.gather(
                self._vad.process(audio_queue, on_chunk, on_utterance_end),
                self._streaming.run(streaming_queue, on_text, on_newline, on_interim),
            )
        except asyncio.CancelledError:
            logger.info("Pipeline cancelled — shutting down…")
            await streaming_queue.put(None)
        finally:
            await self._shutdown()

    def _start_hotkey_listener(self) -> None:
        try:
            from pynput import keyboard
        except ImportError:
            logger.warning("pynput not installed — Global hotkeys disabled")
            return

        def _on_toggle_visibility() -> None:
            self._overlay_visible = not self._overlay_visible
            self._overlay.set_visible(self._overlay_visible)
            logger.info("Overlay visibility toggled: %s", "VISIBLE" if self._overlay_visible else "HIDDEN")

        hotkeys = keyboard.GlobalHotKeys({
            "<ctrl>+<shift>+h": _on_toggle_visibility
        })
        hotkeys.daemon = True
        hotkeys.start()
        self._hotkey_listener = hotkeys
        logger.info("Global hotkeys registered: Ctrl+Shift+H (Toggle Overlay)")

    async def _shutdown(self) -> None:
        logger.info("Stopping audio capture…")
        await self._capture.stop()
        if self._hotkey_listener is not None:
            self._hotkey_listener.stop()
        if self._transcript_writer is not None:
            self._transcript_writer.flush()
        self._overlay.stop()
        logger.info("LiveRex stopped")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="LiveRex",
        description="Real-time meeting transcription for Ubuntu",
    )
    parser.add_argument(
        "--backend",
        choices=["local", "chirp2"],
        default="chirp2",
        help="Transcription backend (overrides config.yaml).",
    )
    parser.add_argument(
        "--language",
        default="en",
        help="BCP-47 language code.",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--save-transcript",
        action="store_true",
        help="Save session transcript to a timestamped text file",
    )
    parser.add_argument(
        "--transcript-dir",
        default="transcripts",
        help="Directory to save transcripts.",
    )
    return parser.parse_args()


def _main() -> None:
    args = _parse_args()
    setup_logging(debug=args.debug)

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"ERROR: config file not found: {config_path}", file=sys.stderr)
        raise SystemExit(1)

    with open(config_path) as f:
        config: dict[str, Any] = yaml.safe_load(f)

    backend: str = args.backend or config["transcription"].get("backend", "local")
    if args.language:
        config["transcription"]["language"] = args.language

    logger.info("LiveRex starting  backend=%s  language=%s", backend, config["transcription"]["language"])

    _check_audio_device()

    transcript_writer = None
    if args.save_transcript:
        from utils.transcript_writer import TranscriptWriter
        transcript_writer = TranscriptWriter(output_dir=args.transcript_dir, session_name="session")
        logger.info("Transcript saving enabled → %s", transcript_writer.file_path)

    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    qt_app = QApplication(sys.argv)
    pipeline = Pipeline(config, backend, transcript_writer)
    pipeline._overlay.start()

    loop = asyncio.new_event_loop()

    def _run_pipeline() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(pipeline.run())
        except Exception:
            logger.exception("Pipeline error")
        finally:
            qt_app.quit()

    asyncio_thread = threading.Thread(target=_run_pipeline, daemon=True, name="asyncio-pipeline")
    asyncio_thread.start()

    def _cancel_pipeline() -> None:
        for task in asyncio.all_tasks(loop):
            task.cancel()

    def _sigint_handler(sig, frame) -> None:
        print("\nInterrupted by user", file=sys.stderr)
        loop.call_soon_threadsafe(_cancel_pipeline)
        qt_app.quit()

    signal.signal(signal.SIGINT, _sigint_handler)

    _sig_timer = QTimer()
    _sig_timer.timeout.connect(lambda: None)
    _sig_timer.start(200)

    qt_app.exec()
    loop.call_soon_threadsafe(_cancel_pipeline)
    asyncio_thread.join(timeout=5.0)


if __name__ == "__main__":
    _main()
