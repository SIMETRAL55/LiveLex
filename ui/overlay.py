"""
Massive Glass UI Overlay for LiveRex (PyQt6).
Dark glass aesthetic, IBM Plex Mono transcript, teal accents, two-column layout.
"""

import logging
import time
from datetime import datetime
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import (
    Qt, QObject, pyqtSignal, QPoint, QTimer, 
    QPropertyAnimation, QEasingCurve, QRect, QSize,
)
from PyQt6.QtGui import QFont, QColor, QMouseEvent, QCursor, QPainter, QLinearGradient
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
    QLabel, QFrame, QScrollArea, QPushButton, QGraphicsOpacityEffect,
    QSpacerItem, QSizePolicy
)

logger = logging.getLogger(__name__)

# ── Visual Constants ────────────────────────────────────────────────────────
_BG = "rgba(9, 12, 21, 0.96)"
_ACCENT = "#2dd4bf"          # teal — live dot, current utterance
_TEXT_FULL = "rgba(255,255,255,0.90)"
_TEXT_MID = "rgba(255,255,255,0.40)"
_TEXT_DIM = "rgba(255,255,255,0.18)"
_TEXT_MONO = "IBM Plex Mono, 'Courier New', monospace"
_TEXT_SANS = "'Segoe UI', 'Helvetica Neue', Arial, sans-serif"


# ---------------------------------------------------------------------------

# Thread-safe signal bridge
# ---------------------------------------------------------------------------

class _Signals(QObject):
    """Carries cross-thread Qt signals for overlay updates."""
    append_text = pyqtSignal(str)
    append_newline = pyqtSignal()
    set_interim_text = pyqtSignal(str)
    set_visible = pyqtSignal(bool)
    stop_app = pyqtSignal()
    set_running = pyqtSignal(bool)             # pause/resume


# ---------------------------------------------------------------------------
# Discrete Utterance Row
# ---------------------------------------------------------------------------

class _UtteranceRow(QFrame):
    """A single row in the transcript column."""

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("utteranceRow")
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(12)

        # Fixed left accent bar
        self._accent_bar = QFrame()
        self._accent_bar.setFixedWidth(3)
        self._accent_bar.setFixedHeight(24)
        self._accent_bar.setStyleSheet(f"background-color: {_ACCENT}; border-radius: 1px;")
        self._accent_opacity = QGraphicsOpacityEffect(self._accent_bar)
        self._accent_bar.setGraphicsEffect(self._accent_opacity)
        layout.addWidget(self._accent_bar, 0, Qt.AlignmentFlag.AlignTop)

        # Text content layout
        content_layout = QVBoxLayout()
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(2)

        # Speaker label (placeholder)
        self._speaker_label = QLabel("SPEAKER")
        self._speaker_label.setFont(QFont(_TEXT_MONO, 9, QFont.Weight.Bold))
        self._speaker_label.setStyleSheet(f"color: {_ACCENT}; letter-spacing: 0.5px;")
        content_layout.addWidget(self._speaker_label)

        # Text label
        self._text_label = QLabel()
        self._text_label.setWordWrap(True)
        self._text_label.setFont(QFont(_TEXT_MONO, 14))
        self._text_label.setTextFormat(Qt.TextFormat.RichText)
        self._text_label.setStyleSheet("line-height: 140%;")
        
        self._text_opacity = QGraphicsOpacityEffect(self._text_label)
        self._text_label.setGraphicsEffect(self._text_opacity)
        
        content_layout.addWidget(self._text_label)
        layout.addLayout(content_layout, 1)

        self._full_text = text
        self._age = "current"
        self._flash_anim = QPropertyAnimation(self._text_opacity, b"opacity")
        self._flash_anim.setDuration(180)
        self._flash_anim.setStartValue(0.0)
        self._flash_anim.setEndValue(1.0)

    def update_text(self, committed: str, interim: str) -> None:
        new_full = (committed + interim).strip()
        if new_full == self._full_text:
            return
            
        self._full_text = new_full
        
        committed_html = f'<span style="color:{_TEXT_FULL};">{committed}</span>'
        interim_html = f'<span style="color:rgba(255,255,255,0.38);font-style:italic;"> {interim}</span>' if interim else ""
        self._text_label.setText(committed_html + interim_html)
        
        # Briefly flash new words
        if committed and self._age == "current":
            self._flash_anim.stop()
            self._flash_anim.start()

    def set_age(self, age: str) -> None:
        """age: 'current', 'recent', or 'old'"""
        self._age = age
        if age == "current":
            self._text_label.setStyleSheet(f"color: {_TEXT_FULL}; line-height: 140%;")
            self._accent_opacity.setOpacity(0.9)
            self._speaker_label.setStyleSheet(f"color: {_ACCENT}; letter-spacing: 0.5px;")
        elif age == "recent":
            self._text_label.setStyleSheet(f"color: {_TEXT_MID}; line-height: 140%;")
            self._accent_opacity.setOpacity(0.25)
            self._speaker_label.setStyleSheet(f"color: rgba(255,255,255,0.25); letter-spacing: 0.5px;")
        else: # old
            self._text_label.setStyleSheet(f"color: {_TEXT_DIM}; line-height: 140%;")
            self._accent_opacity.setOpacity(0.0)
            self._speaker_label.setStyleSheet(f"color: rgba(255,255,255,0.12); letter-spacing: 0.5px;")


# ---------------------------------------------------------------------------
# HUD Bar
# ---------------------------------------------------------------------------

class _HUDBar(QFrame):
    """Top HUD bar for window drag and controls."""
    pause_toggled = pyqtSignal(bool)
    collapse_toggled = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(34)
        self.setStyleSheet("background: transparent;")
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(10)

        # Pulse Dot
        self._pulse_dot = QLabel()
        self._pulse_dot.setFixedSize(6, 6)
        self._pulse_dot.setStyleSheet(f"background-color: {_ACCENT}; border-radius: 3px;")
        self._pulse_opacity = QGraphicsOpacityEffect(self._pulse_dot)
        self._pulse_dot.setGraphicsEffect(self._pulse_opacity)
        layout.addWidget(self._pulse_dot)

        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._toggle_pulse)
        self._pulse_timer.start(1100)
        self._pulse_state = True

        # Wordmark
        wordmark = QLabel("LIVEREX")
        wordmark.setFont(QFont(_TEXT_SANS, 10, QFont.Weight.Bold))
        wordmark.setStyleSheet(f"color: {_ACCENT}; letter-spacing: 1px;")
        wordmark_opacity = QGraphicsOpacityEffect(wordmark)
        wordmark_opacity.setOpacity(0.75)
        wordmark.setGraphicsEffect(wordmark_opacity)
        layout.addWidget(wordmark)

        # Separator
        sep1 = QFrame()
        sep1.setFixedSize(1, 14)
        sep1.setStyleSheet("background-color: rgba(255,255,255,0.08);")
        layout.addWidget(sep1)

        # Latency
        self._latency_label = QLabel("~0ms")
        self._latency_label.setFont(QFont(_TEXT_MONO, 10))
        self._latency_label.setStyleSheet("color: rgba(255,255,255,0.25);")
        layout.addWidget(self._latency_label)

        # Paced text (visible in pill mode)
        self._pill_text = QLabel("")
        self._pill_text.setFont(QFont(_TEXT_MONO, 10))
        self._pill_text.setStyleSheet("color: rgba(255,255,255,0.5);")
        self._pill_text.hide()
        layout.addWidget(self._pill_text, 1)

        layout.addStretch()

        # Controls
        self._pause_btn = QPushButton("⏸")
        self._pause_btn.setFixedSize(26, 26)
        self._pause_btn.setStyleSheet("QPushButton { border: none; color: white; background: transparent; font-size: 14px; }")
        self._pause_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pause_btn.clicked.connect(self._toggle_pause)
        layout.addWidget(self._pause_btn)
        self._running = True

        self._collapse_btn = QPushButton("—")
        self._collapse_btn.setFixedSize(26, 26)
        self._collapse_btn.setStyleSheet("QPushButton { border: none; color: white; background: transparent; font-size: 14px; }")
        self._collapse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._collapse_btn.clicked.connect(self.collapse_toggled.emit)
        layout.addWidget(self._collapse_btn)

    def _toggle_pulse(self):
        self._pulse_state = not self._pulse_state
        self._pulse_opacity.setOpacity(1.0 if self._pulse_state else 0.5)

    def _toggle_pause(self):
        self._running = not self._running
        self._pause_btn.setText("⏸" if self._running else "▶")
        self.pause_toggled.emit(self._running)
        if not self._running:
            self._latency_label.setText("paused")

    def set_latency(self, ms: int):
        if self._running:
            self._latency_label.setText(f"~{ms}ms")

    def set_pill_text(self, text: str):
        if text:
            trunc = (text[:47] + "...") if len(text) > 50 else text
            self._pill_text.setText(trunc)
            self._pill_text.show()
        else:
            self._pill_text.hide()


# ---------------------------------------------------------------------------
# Main Overlay Window
# ---------------------------------------------------------------------------

class _OverlayWindow(QWidget):
    """The actual PyQt6 window."""
    def __init__(self, config: dict[str, Any], signals: _Signals, facade: 'CaptionOverlay') -> None:
        super().__init__()
        self._config = config
        self._signals = signals
        self._facade = facade

        # Visual state
        self._pill_mode = False
        self._running = True
        self._last_final_time = 0

        # Window Config
        x = config.get("x", 100)
        y = config.get("y", 800)
        self._default_width = 680
        self._default_height = 340
        self._opacity = config.get("opacity", 0.98)

        self.setWindowTitle("LiveLex Overlay")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowOpacity(self._opacity)
        self.setGeometry(x, y, self._default_width, self._default_height)
        self.setMinimumSize(520, 200)

        # Drag/Resize state
        self._drag_pos = None
        self._resizing = None

        # ── Root layout ───────────────────────────────────────────────
        self._root_layout = QVBoxLayout(self)
        self._root_layout.setContentsMargins(0, 0, 0, 0)
        self._root_layout.setSpacing(0)

        # ── Glass Container ───────────────────────────────────────────
        self._container = QFrame()
        self._container.setObjectName("glassContainer")
        self._container.setStyleSheet(f"""
            QFrame#glassContainer {{ 
                background-color: {_BG}; 
                border-radius: 14px; 
                border: 1px solid rgba(255, 255, 255, 0.07);
            }}
        """)
        self._root_layout.addWidget(self._container)

        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(0, 0, 0, 0)
        self._container_layout.setSpacing(0)

        # ── HUD Bar ───────────────────────────────────────────────────
        self._hud = _HUDBar()
        self._hud.pause_toggled.connect(self._on_pause_toggled)
        self._hud.collapse_toggled.connect(self._on_collapse_toggled)
        self._container_layout.addWidget(self._hud)

        # ── Panel Body ────────────────────────────────────────────────
        self._panel_body = QWidget()
        self._body_layout = QHBoxLayout(self._panel_body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(0)
        self._container_layout.addWidget(self._panel_body, 1)

        # ── Transcript Column ─────────────────────────────────────────
        self._tx_col = QWidget()
        tx_layout = QVBoxLayout(self._tx_col)
        tx_layout.setContentsMargins(16, 0, 16, 16)
        tx_layout.setSpacing(0)

        self._tx_scroll = QScrollArea()
        self._tx_scroll.setWidgetResizable(True)
        self._tx_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._tx_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._tx_scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical {
                background: rgba(255,255,255,0.04);
                width: 4px;
                border-radius: 2px;
            }
            QScrollBar::handle:vertical {
                background: rgba(255,255,255,0.18);
                border-radius: 2px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(255,255,255,0.35);
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)

        self._tx_content = QWidget()
        self._tx_content.setStyleSheet("background: transparent;")
        self._tx_vbox = QVBoxLayout(self._tx_content)
        self._tx_vbox.setContentsMargins(0, 0, 0, 0)
        self._tx_vbox.setSpacing(4)
        self._tx_vbox.addStretch()

        self._tx_scroll.setWidget(self._tx_content)
        tx_layout.addWidget(self._tx_scroll)
        self._body_layout.addWidget(self._tx_col, 1)

        # ── State ─────────────────────────────────────────────────────
        self._utterance_rows: list[_UtteranceRow] = []
        self._current_row: _UtteranceRow | None = None
        self._committed_text = ""
        self._interim_text = ""
        self._utterance_start_time = 0

        # ── Connect signals ───────────────────────────────────────────
        signals.append_text.connect(self._do_append_text)
        signals.append_newline.connect(self._do_append_newline)
        signals.set_interim_text.connect(self._do_set_interim_text)
        signals.set_visible.connect(self._do_set_visible)
        signals.stop_app.connect(self._do_stop)
        signals.set_running.connect(self._on_pause_toggled)

        self.setMouseTracking(True)
        self._container.setMouseTracking(True)
        self._panel_body.setMouseTracking(True)
        self._tx_col.setMouseTracking(True)
        self._container.installEventFilter(self)
        self.show()


    # ------------------------------------------------------------------
    # Logic
    # ------------------------------------------------------------------

    def _on_pause_toggled(self, running: bool):
        self._running = running
        if not running:
            self._interim_text = ""
            if self._current_row:
                self._current_row.update_text(self._committed_text, "")

    def _on_collapse_toggled(self):
        self._pill_mode = not self._pill_mode
        if self._pill_mode:
            self._panel_body.hide()
            self.setFixedHeight(34)
            self._hud.set_pill_text(self._committed_text or "Listening...")
        else:
            self.setFixedHeight(self._default_height)
            self.setMinimumHeight(200)
            self._panel_body.show()
            self._hud.set_pill_text("")

    def _do_append_text(self, text: str):
        if not self._running: return
        if not self._committed_text and not self._interim_text:
            self._utterance_start_time = time.monotonic()
        self._committed_text += text
        self._ensure_current_row()
        self._current_row.update_text(self._committed_text, self._interim_text)
        self._scroll_to_bottom()

    def _do_set_interim_text(self, text: str):
        if not self._running: return
        if not self._committed_text and not self._interim_text:
            self._utterance_start_time = time.monotonic()
        self._interim_text = text
        self._ensure_current_row()
        self._current_row.update_text(self._committed_text, text)
        self._scroll_to_bottom()

    def _do_append_newline(self):
        if not self._running: return
        
        # Calculate latency for HUD: from first chunk to final newline
        if self._utterance_start_time > 0:
            latency = int((time.monotonic() - self._utterance_start_time) * 1000)
            self._hud.set_latency(min(max(latency, 120), 1200))

        if self._current_row:
            self._current_row.update_text(self._committed_text, "")
            self._current_row.set_age("recent")
            
        # Age existing rows — index 0 is the one just committed (recent)
        # everything else becomes old
        for row in self._utterance_rows[1:]:
            row.set_age("old")
            
        # Keep up to 200 utterances — full session history scrollable
        # Oldest rows are at the end of the list now (newest-first order)
        if len(self._utterance_rows) > 200:
            old = self._utterance_rows.pop()  # pop from end = oldest
            self._tx_vbox.removeWidget(old)
            old.deleteLater()

        self._current_row = None
        self._committed_text = ""
        self._interim_text = ""
        self._utterance_start_time = 0
        self._scroll_to_bottom()

    def _ensure_current_row(self):
        if self._current_row is None:
            self._current_row = _UtteranceRow()
            self._utterance_rows.insert(0, self._current_row)
            self._tx_vbox.insertWidget(0, self._current_row)

    def _scroll_to_bottom(self) -> None:
        # Despite the name this now scrolls to TOP since newest is at top
        def _do_scroll():
            bar = self._tx_scroll.verticalScrollBar()
            # Only auto-scroll to top if user is already near the top (within 60px)
            # If they've scrolled down to read older content, leave them there
            if bar.value() <= 60:
                bar.setValue(0)
        QTimer.singleShot(0, _do_scroll)

    def _do_set_visible(self, visible: bool):
        self.show() if visible else self.hide()

    def _do_stop(self):
        QApplication.instance().quit()

    # ── Mouse Interaction ────────────────────────────────────────────

    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        if event.type() == QEvent.Type.MouseMove:
            # Re-run cursor logic using window-relative position
            local_pos = self.mapFromGlobal(event.globalPosition().toPoint())
            if not self._resizing and not self._drag_pos:
                region = self._resize_region(local_pos)
                shapes = {
                    "n":  Qt.CursorShape.SizeVerCursor,
                    "s":  Qt.CursorShape.SizeVerCursor,
                    "w":  Qt.CursorShape.SizeHorCursor,
                    "e":  Qt.CursorShape.SizeHorCursor,
                    "nw": Qt.CursorShape.SizeBDiagCursor,
                    "se": Qt.CursorShape.SizeBDiagCursor,
                    "ne": Qt.CursorShape.SizeFDiagCursor,
                    "sw": Qt.CursorShape.SizeFDiagCursor,
                }
                self.setCursor(shapes.get(region, Qt.CursorShape.ArrowCursor))
        return super().eventFilter(obj, event)

    def _resize_region(self, pos: QPoint) -> str | None:
        """Return resize direction string or None."""
        x, y, w, h = pos.x(), pos.y(), self.width(), self.height()
        edge = 8
        top    = y < edge
        bottom = y > h - edge
        left   = x < edge
        right  = x > w - edge
        
        if top and left:   return "nw"
        if top and right:  return "ne"
        if bottom and left:  return "sw"
        if bottom and right: return "se"
        if top:    return "n"
        if bottom: return "s"
        if left:   return "w"
        if right:  return "e"
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        region = self._resize_region(event.pos())
        if region:
            self._resizing = region
        elif event.pos().y() < 34: # HUD area
            self._drag_pos = event.globalPosition().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._resizing:
            gp = event.globalPosition().toPoint()
            rect = self.geometry()
            if "n" in self._resizing: rect.setTop(gp.y())
            if "s" in self._resizing: rect.setBottom(gp.y())
            if "w" in self._resizing: rect.setLeft(gp.x())
            if "e" in self._resizing: rect.setRight(gp.x())
            if rect.width() >= 520 and rect.height() >= 200:
                self.setGeometry(rect)
        elif self._drag_pos:
            self.move(self.pos() + event.globalPosition().toPoint() - self._drag_pos)
            self._drag_pos = event.globalPosition().toPoint()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_pos = None
        self._resizing = None
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------------------
# Public facade
# ---------------------------------------------------------------------------

class CaptionOverlay:
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._signals: _Signals | None = None

    def start(self) -> None:
        self._signals = _Signals()
        self._window = _OverlayWindow(self._config, self._signals, self)
        logger.info("CaptionOverlay (PyQt6) Massive Glass UI started")

    def stop(self) -> None:
        if self._signals:
            self._signals.stop_app.emit()

    def append_text(self, text: str) -> None:
        if text and self._signals:
            self._signals.append_text.emit(text)

    def append_newline(self) -> None:
        if self._signals:
            self._signals.append_newline.emit()

    def set_interim_text(self, text: str) -> None:
        if self._signals:
            self._signals.set_interim_text.emit(text)

    def set_visible(self, visible: bool) -> None:
        if self._signals:
            self._signals.set_visible.emit(visible)

    def set_running(self, running: bool) -> None:
        if self._signals:
            self._signals.set_running.emit(running)


if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    test_config = {"x": 100, "y": 100, "opacity": 0.95}
    overlay = CaptionOverlay(test_config)
    overlay.start()
    
    # Simulate some activity
    def test_activity():
        overlay.append_text("Hello world, this is a test of the new LiveRex Glass UI.")
        overlay.set_interim_text("and some interim")
        QTimer.singleShot(2000, lambda: overlay.append_newline())
        QTimer.singleShot(3000, lambda: overlay.append_text("Second sentence starting now."))

    QTimer.singleShot(1000, test_activity)
    sys.exit(app.exec())
