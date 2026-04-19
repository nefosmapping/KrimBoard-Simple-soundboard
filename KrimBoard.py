#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# === УЛУЧШЕННАЯ ЗАЩИТА ОТ ЗАСЫПАНИЯ ХУКА v2.1 — ИСПРАВЛЕНИЯ ОТ 19.04.2026 ===

"""
KrimBoard 2.0 — профессиональная звуковая доска (soundboard) на PySide6.
Без микрофона, только воспроизведение звуков с глобальными горячими клавишами.
Улучшенная версия с QTableView, сортировкой, категориями, треем и многим другим.
"""

import sys
import os
import json
import time
import threading
import queue
import logging
import subprocess
from datetime import datetime
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Set, Dict, Any, Callable, Union
from enum import Enum
from collections import defaultdict

import numpy as np
import sounddevice as sd
from pydub import AudioSegment
import keyboard

from PySide6.QtCore import (
    Qt, QTimer, Signal, Slot, QPoint, QRect, QSize, QEvent, QObject, QThread,
    QModelIndex, QPersistentModelIndex, QAbstractTableModel, QSortFilterProxyModel,
    QItemSelectionModel, QMimeData, QProcess
)
from PySide6.QtGui import (
    QAction, QIcon, QFont, QColor, QPalette, QKeySequence, QPixmap, QPainter,
    QBrush, QPen, QFontDatabase, QMouseEvent, QDrag, QDropEvent, QDragMoveEvent,
    QDragEnterEvent
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QToolBar, QStatusBar, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QLineEdit, QComboBox, QCheckBox,
    QSpinBox, QDoubleSpinBox, QSlider, QDialog, QTabWidget, QGroupBox,
    QFormLayout, QMessageBox, QFileDialog, QProgressBar, QFrame, QSplitter,
    QStyle, QStyleOption, QMenu, QMenuBar, QSizePolicy, QButtonGroup,
    QRadioButton, QDialogButtonBox, QTableView, QStyledItemDelegate,
    QStyleOptionButton, QSystemTrayIcon,
)

# ----------------------------------------------------------------------
# Настройка логирования
# ----------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("krimboard.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("KrimBoard2")

# ----------------------------------------------------------------------
# Константы
# ----------------------------------------------------------------------
DEFAULT_VOLUME = 100
DEBOUNCE_TIME = 0.05
AUTO_SAVE_DELAY = 300.0  # секунд
VU_UPDATE_INTERVAL_MS = 50
FADE_OUT_DURATION = 0.2  # секунды для затухания
HEALTH_CHECK_INTERVAL_MS = 4000   # проверка каждые 4 секунды
HEARTBEAT_TIMEOUT_SEC = 12        # отсутствие heartbeat дольше этого -> засыпание

# Поддерживаемые аудио расширения
AUDIO_EXTENSIONS = ('.wav', '.mp3', '.ogg', '.flac', '.m4a', '.aac')

# ----------------------------------------------------------------------
# Поиск FFmpeg
# ----------------------------------------------------------------------
def find_ffmpeg() -> Optional[str]:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    ffmpeg_local = os.path.join(script_dir, "ffmpeg.exe")
    if os.path.exists(ffmpeg_local):
        return ffmpeg_local
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return "ffmpeg"
    except Exception:
        return None

FFMPEG_PATH = find_ffmpeg()
if FFMPEG_PATH is None:
    logger.warning("FFmpeg не найден. Для работы с MP3 и другими форматами установите ffmpeg и добавьте в PATH.")

# ----------------------------------------------------------------------
# Конфигурационные датаклассы
# ----------------------------------------------------------------------
@dataclass
class SoundItem:
    """Представление звукового файла."""
    name: str
    file: str
    key: str = ""
    duration: float = 0.0
    volume: int = DEFAULT_VOLUME          # 0-100
    last_played: Optional[str] = None     # ISO timestamp
    category: str = ""
    order: int = 0                        # для кастомного порядка
    added_date: Optional[str] = None      # дата добавления (ISO timestamp)

    def __post_init__(self):
        # Валидация громкости
        self.volume = max(0, min(100, self.volume))

@dataclass
class OutputDevice:
    index: int
    name: str
    hostapi: str

@dataclass
class AppConfig:
    sounds: List[SoundItem] = field(default_factory=list)
    primary_device: Optional[OutputDevice] = None
    secondary_device: Optional[OutputDevice] = None
    auto_save_enabled: bool = True
    logging_enabled: bool = False
    ignored_keys: Set[str] = field(default_factory=set)
    sound_volume: int = 100               # глобальная громкость
    overlay_enabled: bool = False
    overlay_opacity: float = 0.8
    overlay_font_size: int = 24
    overlay_position: str = "top-right"
    overlay_custom_x: int = 100
    overlay_custom_y: int = 100
    overlay_timeout: float = 3.0          # секунд, 0 - не скрывать автоматически
    global_hotkeys: Dict[str, str] = field(default_factory=dict)
    theme: str = "dark"
    sort_mode: str = "order"              # "name", "duration", "key", "order", "added_date"
    sort_order: int = Qt.AscendingOrder   # 0 = Ascending, 1 = Descending
    enable_global_hotkeys: bool = True
    sleep_overlay_enabled: bool = True    # показывать оверлей при засыпании хука
    attention_sound_enabled: bool = True  # воспроизводить звук внимания при засыпании

# ----------------------------------------------------------------------
# Менеджер конфигурации
# ----------------------------------------------------------------------
class ConfigManager:
    CONFIG_FILE = "krimboard_config.json"

    def __init__(self):
        self.config = AppConfig()
        self.need_save = False
        self.last_change_time = 0
        self.auto_save_timer: Optional[threading.Timer] = None
        self.load()

    def load(self):
        if not os.path.exists(self.CONFIG_FILE):
            logger.info("Файл конфигурации не найден, создаю новый.")
            self.save()
            return

        try:
            with open(self.CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                content = f.read().strip()

            if not content:
                raise ValueError("Файл конфигурации пустой")

            data = json.loads(content)

            # Обработка звуков с обратной совместимостью
            sounds_data = data.get("sounds", [])
            sounds = []
            for s in sounds_data:
                if "added_date" not in s or s["added_date"] is None:
                    s["added_date"] = None
                sounds.append(SoundItem(**s))

            primary = data.get("primary_device")
            if primary:
                primary = OutputDevice(**primary)
            secondary = data.get("secondary_device")
            if secondary:
                secondary = OutputDevice(**secondary)
            ignored = set(data.get("ignored_keys", []))
            global_hotkeys = data.get("global_hotkeys", {})
            theme = data.get("theme", "dark")
            sort_mode = data.get("sort_mode", "order")
            
            # sort_order должен быть int (0 или 1), а не Qt объект
            sort_order = data.get("sort_order", 0)
            if isinstance(sort_order, int):
                sort_order = Qt.AscendingOrder if sort_order == 0 else Qt.DescendingOrder

            # Для обратной совместимости
            overlay_timeout = data.get("overlay_timeout", 3.0)
            enable_global_hotkeys = data.get("enable_global_hotkeys", True)
            sleep_overlay_enabled = data.get("sleep_overlay_enabled", True)
            attention_sound_enabled = data.get("attention_sound_enabled", True)

            self.config = AppConfig(
                sounds=sounds,
                primary_device=primary,
                secondary_device=secondary,
                auto_save_enabled=data.get("auto_save_enabled", True),
                logging_enabled=data.get("logging_enabled", False),
                ignored_keys=ignored,
                sound_volume=data.get("sound_volume", 100),
                overlay_enabled=data.get("overlay_enabled", False),
                overlay_opacity=data.get("overlay_opacity", 0.8),
                overlay_font_size=data.get("overlay_font_size", 24),
                overlay_position=data.get("overlay_position", "top-right"),
                overlay_custom_x=data.get("overlay_custom_x", 100),
                overlay_custom_y=data.get("overlay_custom_y", 100),
                overlay_timeout=overlay_timeout,
                global_hotkeys=global_hotkeys,
                theme=theme,
                sort_mode=sort_mode,
                sort_order=sort_order,
                enable_global_hotkeys=enable_global_hotkeys,
                sleep_overlay_enabled=sleep_overlay_enabled,
                attention_sound_enabled=attention_sound_enabled
            )
            
            logger.info("Конфигурация загружена успешно.")
            
        except json.JSONDecodeError as e:
            logger.error(f"JSON повреждён: {e} (строка {e.lineno}, символ {e.colno})")
            import shutil, time
            broken_backup = f"{self.CONFIG_FILE}.broken_{int(time.time())}"
            shutil.copy2(self.CONFIG_FILE, broken_backup)
            logger.warning(f"Создан бэкап повреждённого конфига: {broken_backup}")
            logger.warning("Создаю новый чистый конфиг.")
            self.config = AppConfig()
            self.save()
            
        except Exception as e:
            logger.error(f"Ошибка загрузки конфига: {e}")
            self.config = AppConfig()
            self.save()

    def save(self):
        try:
            # Создаём бэкап перед записью
            if os.path.exists(self.CONFIG_FILE):
                backup_path = self.CONFIG_FILE + ".bak"
                import shutil
                shutil.copy2(self.CONFIG_FILE, backup_path)

            # Преобразуем Qt.SortOrder в обычное int для JSON
            sort_order_int = 0 if self.config.sort_order == Qt.AscendingOrder else 1

            data = {
                "sounds": [asdict(s) for s in self.config.sounds],
                "primary_device": asdict(self.config.primary_device) if self.config.primary_device else None,
                "secondary_device": asdict(self.config.secondary_device) if self.config.secondary_device else None,
                "auto_save_enabled": self.config.auto_save_enabled,
                "logging_enabled": self.config.logging_enabled,
                "ignored_keys": list(self.config.ignored_keys),
                "sound_volume": self.config.sound_volume,
                "overlay_enabled": self.config.overlay_enabled,
                "overlay_opacity": self.config.overlay_opacity,
                "overlay_font_size": self.config.overlay_font_size,
                "overlay_position": self.config.overlay_position,
                "overlay_custom_x": self.config.overlay_custom_x,
                "overlay_custom_y": self.config.overlay_custom_y,
                "overlay_timeout": self.config.overlay_timeout,
                "global_hotkeys": self.config.global_hotkeys,
                "theme": self.config.theme,
                "sort_mode": self.config.sort_mode,
                "sort_order": sort_order_int,
                "enable_global_hotkeys": self.config.enable_global_hotkeys,
                "sleep_overlay_enabled": self.config.sleep_overlay_enabled,
                "attention_sound_enabled": self.config.attention_sound_enabled
            }

            # Атомарная запись через временный файл
            temp_file = self.CONFIG_FILE + ".tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            
            # Заменяем оригинал
            if os.path.exists(self.CONFIG_FILE):
                os.remove(self.CONFIG_FILE)
            os.rename(temp_file, self.CONFIG_FILE)

            self.need_save = False
            logger.info("Конфигурация сохранена успешно.")
            
        except Exception as e:
            logger.error(f"Ошибка сохранения конфига: {e}")

    def mark_changed(self):
        self.need_save = True
        self.last_change_time = time.time()
        if self.config.auto_save_enabled:
            if self.auto_save_timer and self.auto_save_timer.is_alive():
                return
            self.auto_save_timer = threading.Timer(AUTO_SAVE_DELAY, self.auto_save)
            self.auto_save_timer.daemon = True
            self.auto_save_timer.start()

    def auto_save(self):
        if self.need_save and self.config.auto_save_enabled:
            self.save()

    def import_config(self, filepath: str):
        """Импорт конфига из файла."""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Пересоздаём звуки с учётом возможного отсутствия added_date
            sounds_data = data.get("sounds", [])
            sounds = []
            for s in sounds_data:
                if "added_date" not in s:
                    s["added_date"] = None
                sounds.append(SoundItem(**s))

            # Остальные поля
            primary = data.get("primary_device")
            if primary:
                primary = OutputDevice(**primary)
            secondary = data.get("secondary_device")
            if secondary:
                secondary = OutputDevice(**secondary)
            ignored = set(data.get("ignored_keys", []))
            global_hotkeys = data.get("global_hotkeys", {})
            theme = data.get("theme", "dark")
            sort_mode = data.get("sort_mode", "order")
            sort_order = data.get("sort_order", Qt.AscendingOrder)
            overlay_timeout = data.get("overlay_timeout", 3.0)
            enable_global_hotkeys = data.get("enable_global_hotkeys", True)
            sleep_overlay_enabled = data.get("sleep_overlay_enabled", True)
            attention_sound_enabled = data.get("attention_sound_enabled", True)

            self.config = AppConfig(
                sounds=sounds,
                primary_device=primary,
                secondary_device=secondary,
                auto_save_enabled=data.get("auto_save_enabled", True),
                logging_enabled=data.get("logging_enabled", False),
                ignored_keys=ignored,
                sound_volume=data.get("sound_volume", 100),
                overlay_enabled=data.get("overlay_enabled", False),
                overlay_opacity=data.get("overlay_opacity", 0.8),
                overlay_font_size=data.get("overlay_font_size", 24),
                overlay_position=data.get("overlay_position", "top-right"),
                overlay_custom_x=data.get("overlay_custom_x", 100),
                overlay_custom_y=data.get("overlay_custom_y", 100),
                overlay_timeout=overlay_timeout,
                global_hotkeys=global_hotkeys,
                theme=theme,
                sort_mode=sort_mode,
                sort_order=sort_order,
                enable_global_hotkeys=enable_global_hotkeys,
                sleep_overlay_enabled=sleep_overlay_enabled,
                attention_sound_enabled=attention_sound_enabled
            )
            self.save()
            return True
        except Exception as e:
            logger.error(f"Ошибка импорта конфига: {e}")
            return False

    def export_config(self, filepath: str):
        """Экспорт конфига в файл."""
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                data = {
                    "sounds": [asdict(s) for s in self.config.sounds],
                    "primary_device": asdict(self.config.primary_device) if self.config.primary_device else None,
                    "secondary_device": asdict(self.config.secondary_device) if self.config.secondary_device else None,
                    "auto_save_enabled": self.config.auto_save_enabled,
                    "logging_enabled": self.config.logging_enabled,
                    "ignored_keys": list(self.config.ignored_keys),
                    "sound_volume": self.config.sound_volume,
                    "overlay_enabled": self.config.overlay_enabled,
                    "overlay_opacity": self.config.overlay_opacity,
                    "overlay_font_size": self.config.overlay_font_size,
                    "overlay_position": self.config.overlay_position,
                    "overlay_custom_x": self.config.overlay_custom_x,
                    "overlay_custom_y": self.config.overlay_custom_y,
                    "overlay_timeout": self.config.overlay_timeout,
                    "global_hotkeys": self.config.global_hotkeys,
                    "theme": self.config.theme,
                    "sort_mode": self.config.sort_mode,
                    "sort_order": 0 if self.config.sort_order == Qt.AscendingOrder else 1,
                    "enable_global_hotkeys": self.config.enable_global_hotkeys,
                    "sleep_overlay_enabled": self.config.sleep_overlay_enabled,
                    "attention_sound_enabled": self.config.attention_sound_enabled
                }
                json.dump(data, f, ensure_ascii=False, indent=4)
            return True
        except Exception as e:
            logger.error(f"Ошибка экспорта конфига: {e}")
            return False

# ----------------------------------------------------------------------
# Менеджер воспроизведения звуков
# ----------------------------------------------------------------------
class PlaybackThread(QThread):
    """Поток воспроизведения одного звука."""
    finished = Signal()
    vu_level = Signal(float)
    error = Signal(str)

    def __init__(self, sound: SoundItem, devices: List[int], global_volume: int, fade_out: bool = False):
        super().__init__()
        self.sound = sound
        self.devices = devices
        self.global_volume = global_volume
        self.fade_out = fade_out
        self._is_cancelled = False
        self._streams = []

    def run(self):
        try:
            if not os.path.exists(self.sound.file):
                self.error.emit(f"Файл не найден: {self.sound.file}")
                return

            if FFMPEG_PATH and FFMPEG_PATH != "ffmpeg":
                AudioSegment.converter = FFMPEG_PATH

            audio = AudioSegment.from_file(self.sound.file)
            samples = np.array(audio.get_array_of_samples())
            if audio.channels == 2:
                samples = samples.reshape((-1, 2))
            samples_float = samples.astype(np.float32) / (2 ** (8 * audio.sample_width - 1))

            # Применяем громкость: индивидуальная * глобальная
            volume_factor = (self.sound.volume / 100.0) * (self.global_volume / 100.0)
            samples_float *= volume_factor

            samplerate = audio.frame_rate
            channels = audio.channels

            # Создаём потоки для всех устройств
            self._streams = []
            for dev_idx in self.devices:
                stream = sd.OutputStream(
                    samplerate=samplerate,
                    device=dev_idx,
                    channels=channels,
                    callback=self._make_callback(samples_float, samplerate, channels)
                )
                stream.start()
                self._streams.append(stream)

            # Ждём завершения
            while any(s.active for s in self._streams) and not self._is_cancelled:
                sd.sleep(50)

            # Остановка
            for s in self._streams:
                s.stop()
                s.close()
            self._streams.clear()
            self.finished.emit()

        except Exception as e:
            logger.exception(f"Ошибка воспроизведения {self.sound.file}: {e}")
            self.error.emit(str(e))
            self.finished.emit()

    def _make_callback(self, samples: np.ndarray, samplerate: int, channels: int):
        pos = 0
        vu_interval = int(samplerate * 0.05)
        next_vu = vu_interval

        def callback(outdata, frames, time, status):
            nonlocal pos, next_vu
            if status:
                logger.warning(f"Статус воспроизведения: {status}")

            if self._is_cancelled:
                raise sd.CallbackStop

            remaining = len(samples) - pos
            if remaining <= 0:
                raise sd.CallbackStop

            take = min(frames, remaining)
            chunk = samples[pos:pos+take]

            # Применяем fade-out если нужно
            if self.fade_out and remaining < samplerate * FADE_OUT_DURATION:
                fade_factor = max(0, remaining / (samplerate * FADE_OUT_DURATION))
                chunk = chunk * fade_factor

            if chunk.ndim == 1:
                chunk = chunk.reshape(-1, 1)
            outdata[:take] = chunk
            if take < frames:
                outdata[take:] = 0
                raise sd.CallbackStop

            if pos >= next_vu:
                start = max(0, pos - vu_interval)
                segment = samples[start:pos]
                if len(segment) > 0:
                    rms = float(np.sqrt(np.mean(segment**2)))
                    self.vu_level.emit(rms)
                next_vu += vu_interval

            pos += take

        return callback

    def cancel(self, fade_out: bool = True):
        """Остановить воспроизведение."""
        self._is_cancelled = True
        self.fade_out = fade_out


class SoundManager(QObject):
    """Управление воспроизведением звуков."""
    playback_started = Signal(str)      # имя звука
    playback_stopped = Signal()
    vu_level_updated = Signal(float)    # суммарный уровень

    def __init__(self, config: ConfigManager):
        super().__init__()
        self.config = config
        self.active_threads: Dict[int, PlaybackThread] = {}  # id потока -> поток
        self.active_sounds: Dict[str, List[PlaybackThread]] = defaultdict(list)  # по имени звука
        self.lock = threading.RLock()
        self._vu_aggregator_timer = QTimer()
        self._vu_aggregator_timer.timeout.connect(self._aggregate_vu_levels)
        self._vu_aggregator_timer.start(VU_UPDATE_INTERVAL_MS)
        self._current_vu_levels: Dict[int, float] = {}  # thread id -> level

    def play(self, sound: SoundItem):
        """Запустить воспроизведение звука (неблокирующе)."""
        devices = []
        if self.config.config.primary_device:
            devices.append(self.config.config.primary_device.index)
        if self.config.config.secondary_device and self.config.config.secondary_device.index != (
                self.config.config.primary_device.index if self.config.config.primary_device else None):
            devices.append(self.config.config.secondary_device.index)
        if not devices:
            logger.warning("Нет устройств вывода для воспроизведения.")
            return

        thread = PlaybackThread(sound, devices, self.config.config.sound_volume)
        thread.finished.connect(lambda tid=id(thread): self._on_playback_finished(tid))
        thread.vu_level.connect(lambda level, tid=id(thread): self._on_vu_level(tid, level))
        thread.error.connect(lambda msg: logger.error(msg))

        with self.lock:
            self.active_threads[id(thread)] = thread
            self.active_sounds[sound.name].append(thread)

        thread.start()

        # Обновляем last_played
        sound.last_played = datetime.now().isoformat()
        self.config.mark_changed()

        # Логирование
        if self.config.config.logging_enabled:
            self._log_play(sound)

        self.playback_started.emit(sound.name)

    def stop_sound(self, sound_name: str, fade_out: bool = True):
        """Остановить все воспроизведения конкретного звука."""
        with self.lock:
            threads = self.active_sounds.get(sound_name, [])[:]
            for thread in threads:
                thread.cancel(fade_out)
                if id(thread) in self.active_threads:
                    del self.active_threads[id(thread)]
            if sound_name in self.active_sounds:
                del self.active_sounds[sound_name]

    def stop_all(self, fade_out: bool = True):
        """Остановить все воспроизведения."""
        with self.lock:
            for thread in list(self.active_threads.values()):
                thread.cancel(fade_out)
            self.active_threads.clear()
            self.active_sounds.clear()
        self.playback_stopped.emit()
        logger.info("Все звуки остановлены.")

    def _on_playback_finished(self, thread_id: int):
        with self.lock:
            thread = self.active_threads.pop(thread_id, None)
            if thread:
                # Удаляем из списка по имени
                sound_name = thread.sound.name
                if sound_name in self.active_sounds:
                    self.active_sounds[sound_name] = [t for t in self.active_sounds[sound_name] if id(t) != thread_id]
                    if not self.active_sounds[sound_name]:
                        del self.active_sounds[sound_name]
                # Удаляем уровень VU
                self._current_vu_levels.pop(thread_id, None)

        # Проверяем, остались ли активные потоки
        if not self.active_threads:
            self.playback_stopped.emit()

    def _on_vu_level(self, thread_id: int, level: float):
        self._current_vu_levels[thread_id] = level

    def _aggregate_vu_levels(self):
        """Суммируем RMS от всех потоков."""
        if not self._current_vu_levels:
            self.vu_level_updated.emit(0.0)
            return
        # Суммируем квадраты и берём корень
        sum_sq = sum(v**2 for v in self._current_vu_levels.values())
        rms = np.sqrt(sum_sq)
        self.vu_level_updated.emit(min(1.0, rms))  # ограничиваем 1.0

    def _log_play(self, sound: SoundItem):
        try:
            entry = {
                "timestamp": datetime.now().isoformat(),
                "combination": sound.key,
                "file": sound.file,
                "name": sound.name,
                "category": sound.category
            }
            with open("krimboard_log.json", "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"Ошибка записи лога: {e}")

# ----------------------------------------------------------------------
# Окно оверлея
# ----------------------------------------------------------------------
class OverlayWindow(QWidget):
    def __init__(self, config: ConfigManager):
        super().__init__()
        self.config = config
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("background: transparent;")

        self.label = QLabel("")
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet(
            "color: white; background-color: rgba(0, 0, 0, 150); padding: 10px; border-radius: 5px;"
        )

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label)
        self.setLayout(layout)

        self.update_font()
        self.setWindowOpacity(self.config.config.overlay_opacity)

        # Таймер автоматического скрытия
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.hide)

        self.hide()

    def set_text(self, text: str):
        self.label.setText(f"▶ {text}")
        self.adjustSize()
        self.update_position()

    def update_font(self):
        font = QFont("Arial", self.config.config.overlay_font_size)
        self.label.setFont(font)

    def update_position(self):
        pos = self.config.config.overlay_position
        screen = QApplication.primaryScreen().availableGeometry()
        w = self.width()
        h = self.height()

        if pos == "top-left":
            x, y = 0, 0
        elif pos == "top-right":
            x, y = screen.width() - w, 0
        elif pos == "bottom-left":
            x, y = 0, screen.height() - h
        elif pos == "bottom-right":
            x, y = screen.width() - w, screen.height() - h
        elif pos == "custom":
            x, y = self.config.config.overlay_custom_x, self.config.config.overlay_custom_y
        else:
            x, y = 100, 100

        self.move(x, y)

    def set_opacity(self, value: float):
        self.setWindowOpacity(value)

    def show_overlay(self):
        if self.config.config.overlay_enabled and self.label.text():
            self.show()
            # Запускаем или перезапускаем таймер скрытия
            if self.config.config.overlay_timeout > 0:
                self.hide_timer.start(int(self.config.config.overlay_timeout * 1000))
        else:
            self.hide()

# ----------------------------------------------------------------------
# Оверлей предупреждения о засыпании хука
# ----------------------------------------------------------------------
class SleepNotificationOverlay(QWidget):
    """Яркое предупреждение о том, что глобальный хук перестал работать."""
    restart_requested = Signal()  # сигнал для перезапуска приложения

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool |
            Qt.WindowDoesNotAcceptFocus  # не перехватывать фокус
        )
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setStyleSheet("background-color: #FF4500; border: 3px solid #8B0000; border-radius: 15px;")

        layout = QVBoxLayout()
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(20)

        # Текст предупреждения
        warning_label = QLabel("⚠️ KrimBoard заснул\nГорячие клавиши не работают ⚠️")
        warning_label.setAlignment(Qt.AlignCenter)
        warning_label.setStyleSheet("color: white; font-size: 28px; font-weight: bold;")
        layout.addWidget(warning_label)

        # Кнопка перезапуска
        self.restart_btn = QPushButton("🗘 Перезапустить приложение")
        self.restart_btn.setStyleSheet("""
            QPushButton {
                background-color: #FFFFFF;
                color: #FF4500;
                font-size: 22px;
                font-weight: bold;
                padding: 15px;
                border-radius: 10px;
                border: 2px solid #8B0000;
            }
            QPushButton:hover {
                background-color: #FFE4E1;
            }
            QPushButton:pressed {
                background-color: #FFC0CB;
            }
        """)
        self.restart_btn.clicked.connect(self.restart_requested.emit)
        layout.addWidget(self.restart_btn)

        # Кнопка закрытия оверлея (временно скрыть)
        close_btn = QPushButton("✖ Закрыть (временно)")
        close_btn.setStyleSheet("""
            QPushButton {
                background-color: #A9A9A9;
                color: white;
                font-size: 16px;
                padding: 10px;
                border-radius: 8px;
            }
            QPushButton:hover {
                background-color: #C0C0C0;
            }
        """)
        close_btn.clicked.connect(self.hide)
        layout.addWidget(close_btn)

        self.setLayout(layout)
        self.adjustSize()

        # Размещаем по центру экрана
        screen_geo = QApplication.primaryScreen().availableGeometry()
        self.move(screen_geo.center() - self.rect().center())

        self.hide()

    def show_overlay(self):
        """Показать оверлей с анимацией появления."""
        self.show()
        self.raise_()
        self.activateWindow()

# ----------------------------------------------------------------------
# Поток для глобального перехвата клавиш (keyboard.hook в отдельном потоке)
# ----------------------------------------------------------------------
class KeyboardHookThread(QThread):
    """Поток, в котором работает глобальный хук клавиатуры."""
    key_pressed = Signal(str)   # имя клавиши (keyboard.name)
    key_released = Signal(str)
    heartbeat = Signal()        # сигнал "я жив" (вызывается периодически)

    def __init__(self):
        super().__init__()
        self.hook_handler = None
        self._is_running = False

    def run(self):
        """Запускает хук и блокируется до остановки."""
        try:
            keyboard.unhook_all()  # очищаем старые хуки

            self.hook_handler = keyboard.hook(self._on_key_event)
            self._is_running = True
            logger.info("Keyboard hook thread started.")

            # Вместо exec() используем простой цикл — он стабильнее с keyboard
            while self._is_running:
                time.sleep(0.05)          # не жрёт CPU
                # Heartbeat каждые ~2 секунды
                if int(time.time()) % 2 == 0:
                    self.heartbeat.emit()

            logger.info("Keyboard hook thread loop finished.")
        except Exception as e:
            logger.error(f"Ошибка в потоке хука клавиатуры: {e}")
        finally:
            self._cleanup()
            self._is_running = False
            logger.info("Keyboard hook thread stopped.")

    def _cleanup(self):
        if self.hook_handler:
            try:
                keyboard.unhook(self.hook_handler)
            except:
                pass
        self.hook_handler = None

    def _on_key_event(self, event):
        """Вызывается библиотекой keyboard при каждом событии."""
        try:
            if event.event_type == keyboard.KEY_DOWN:
                self.key_pressed.emit(event.name)
            elif event.event_type == keyboard.KEY_UP:
                self.key_released.emit(event.name)
        except Exception as e:
            logger.error(f"Ошибка при обработке события клавиши: {e}")

    def stop(self):
        """Останавливает хук и завершает поток."""
        self._is_running = False
        if self.hook_handler:
            try:
                keyboard.unhook(self.hook_handler)
            except Exception as e:
                logger.warning(f"Ошибка при отключении хука: {e}")
            self.hook_handler = None
        self.quit()
        self.wait(1500)  # даём потоку время завершиться

# ----------------------------------------------------------------------
# Менеджер горячих клавиш
# ----------------------------------------------------------------------
class HotkeyManager(QObject):
    def __init__(self, config: ConfigManager, sound_manager: SoundManager,
                 overlay: OverlayWindow, app_actions: Dict[str, Callable], main_window):
        super().__init__()
        self.config = config
        self.sound_manager = sound_manager
        self.overlay = overlay
        self.app_actions = app_actions
        self.main_window = main_window  # ссылка на главное окно для переподключения сигналов
        self.sound_hotkeys: Dict[frozenset, SoundItem] = {}
        self.global_hotkey_handlers = []
        self.hook_thread: Optional[KeyboardHookThread] = None
        self.current_keys = set()
        self.last_triggered_combo = None
        self.last_trigger_time = 0

        self.setup_sound_hotkeys()
        self.setup_global_hotkeys()

    def setup_sound_hotkeys(self):
        self.sound_hotkeys.clear()
        for s in self.config.config.sounds:
            if s.key:
                keys = set(s.key.lower().split('+'))
                self.sound_hotkeys[frozenset(keys)] = s

    def setup_global_hotkeys(self):
        # Удалить старые глобальные горячие клавиши (те, что через keyboard.add_hotkey)
        for handler in self.global_hotkey_handlers:
            try:
                keyboard.remove_hotkey(handler)
            except:
                pass
        self.global_hotkey_handlers.clear()

        if not self.config.config.enable_global_hotkeys:
            return

        for action, combo in self.config.config.global_hotkeys.items():
            if not combo:
                continue
            try:
                handler = keyboard.add_hotkey(combo, self._make_global_callback(action))
                self.global_hotkey_handlers.append(handler)
                logger.info(f"Зарегистрирована глобальная клавиша {combo} для действия {action}")
            except Exception as e:
                logger.error(f"Не удалось зарегистрировать глобальную клавишу {combo}: {e}")

    def _make_global_callback(self, action: str):
        def callback():
            try:
                self.app_actions[action]()
            except Exception as e:
                logger.error(f"Ошибка выполнения действия {action}: {e}")
        return callback

    def start_global_hook(self):
        """Запускает поток с хуком клавиатуры, если включено в настройках."""
        if not self.config.config.enable_global_hotkeys:
            logger.info("Глобальные горячие клавиши отключены в настройках.")
            return
        if self.hook_thread is not None and self.hook_thread.isRunning():
            logger.warning("Поток хука уже запущен.")
            return

        self.hook_thread = KeyboardHookThread()
        self.hook_thread.key_pressed.connect(self._on_key_pressed)
        self.hook_thread.key_released.connect(self._on_key_released)
        self.hook_thread.heartbeat.connect(self._on_hook_heartbeat)
        self.hook_thread.start()
        logger.info("Глобальный хук клавиатуры запущен в отдельном потоке.")

    def stop_global_hook(self):
        """Останавливает поток хука и очищает состояние."""
        if self.hook_thread is not None:
            self.hook_thread.stop()
            self.hook_thread = None
            self.current_keys.clear()
            self.last_triggered_combo = None
            logger.info("Глобальный хук клавиатуры остановлен.")

    @Slot(str)
    def _on_key_pressed(self, key_name: str):
        """Слот вызывается при нажатии клавиши (из потока хука)."""
        self.current_keys.add(key_name)
        ignored = self.config.config.ignored_keys
        active_keys = self.current_keys - ignored
        current_frozen = frozenset(active_keys)
        if current_frozen in self.sound_hotkeys:
            now = time.time()
            if now - self.last_trigger_time >= DEBOUNCE_TIME:
                self.last_trigger_time = now
                if current_frozen != self.last_triggered_combo:
                    self.last_triggered_combo = current_frozen
                    sound = self.sound_hotkeys[current_frozen]
                    self.sound_manager.play(sound)
        else:
            self.last_triggered_combo = None

    @Slot(str)
    def _on_key_released(self, key_name: str):
        """Слот вызывается при отпускании клавиши."""
        self.current_keys.discard(key_name)
        self.last_triggered_combo = None

    @Slot()
    def _on_hook_heartbeat(self):
        """Принимаем heartbeat от хука, будет переподключено в главном окне."""
        # Этот слот просто существует, чтобы главное окно могло к нему подключиться.
        pass

    def restart_hotkeys(self):
        """Перезапуск глобального хука и горячих клавиш."""
        self.stop_global_hook()
        self.setup_global_hotkeys()
        self.start_global_hook()
        # Переподключаем heartbeat к главному окну
        if self.main_window is not None:
            self.main_window.reconnect_heartbeat()
        logger.info("Обработчик клавиш перезапущен.")

# ----------------------------------------------------------------------
# Модель данных для звуков (QAbstractTableModel)
# ----------------------------------------------------------------------
class SoundsTableModel(QAbstractTableModel):
    """Модель для отображения списка звуков."""

    COL_NAMES = ["Название", "Длит.", "Клавиша", "Категория", "Громк.", "Посл. воспр.", "Play", "Stop", "Дата добавл."]
    COL_NAME = 0
    COL_DURATION = 1
    COL_KEY = 2
    COL_CATEGORY = 3
    COL_VOLUME = 4
    COL_LAST_PLAYED = 5
    COL_PLAY = 6
    COL_STOP = 7
    COL_ADDED_DATE = 8

    dataChanged = Signal(QModelIndex, QModelIndex, list)

    def __init__(self, config: ConfigManager, parent=None):
        super().__init__(parent)
        self.config = config
        self._sounds: List[SoundItem] = self.config.config.sounds.copy()
        self._sort_column = 0
        self._sort_order = Qt.AscendingOrder

    def rowCount(self, parent=QModelIndex()):
        return len(self._sounds) if not parent.isValid() else 0

    def columnCount(self, parent=QModelIndex()):
        return len(self.COL_NAMES)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()
        sound = self._sounds[row]

        if role == Qt.DisplayRole or role == Qt.EditRole:
            if col == self.COL_NAME:
                return sound.name
            elif col == self.COL_DURATION:
                return f"{sound.duration:.1f}s" if sound.duration > 0 else ""
            elif col == self.COL_KEY:
                return sound.key
            elif col == self.COL_CATEGORY:
                return sound.category
            elif col == self.COL_VOLUME:
                return str(sound.volume)
            elif col == self.COL_LAST_PLAYED:
                if sound.last_played:
                    try:
                        dt = datetime.fromisoformat(sound.last_played)
                        return dt.strftime("%Y-%m-%d %H:%M")
                    except:
                        return ""
                return ""
            elif col == self.COL_PLAY:
                return "▶"
            elif col == self.COL_STOP:
                return "■"
            elif col == self.COL_ADDED_DATE:
                if sound.added_date:
                    try:
                        dt = datetime.fromisoformat(sound.added_date)
                        return dt.strftime("%Y-%m-%d %H:%M")
                    except:
                        return ""
                return "—"
        elif role == Qt.TextAlignmentRole:
            if col in (self.COL_DURATION, self.COL_VOLUME, self.COL_LAST_PLAYED, self.COL_ADDED_DATE):
                return Qt.AlignCenter
            elif col in (self.COL_PLAY, self.COL_STOP):
                return Qt.AlignCenter
        elif role == Qt.UserRole:
            # Возвращаем сам объект SoundItem
            return sound
        elif role == Qt.ToolTipRole:
            if col == self.COL_NAME:
                return sound.file
            elif col == self.COL_LAST_PLAYED and sound.last_played:
                return sound.last_played
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if not index.isValid():
            return False
        row = index.row()
        col = index.column()
        sound = self._sounds[row]

        if role == Qt.EditRole:
            if col == self.COL_NAME:
                sound.name = str(value)
            elif col == self.COL_KEY:
                sound.key = str(value)
            elif col == self.COL_CATEGORY:
                sound.category = str(value)
            elif col == self.COL_VOLUME:
                try:
                    vol = int(value)
                    sound.volume = max(0, min(100, vol))
                except:
                    return False
            else:
                return False
            self.config.mark_changed()
            self.dataChanged.emit(index, index, [role])
            return True
        return False

    def flags(self, index):
        flags = super().flags(index)
        col = index.column()
        if col in (self.COL_NAME, self.COL_KEY, self.COL_CATEGORY, self.COL_VOLUME):
            flags |= Qt.ItemIsEditable
        if col == self.COL_PLAY or col == self.COL_STOP:
            flags |= Qt.ItemIsEnabled
        flags |= Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled
        return flags

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.COL_NAMES[section]
        return None

    def sort(self, column, order=Qt.AscendingOrder):
        """Сортировка списка звуков по заданной колонке."""
        self._sort_column = column
        self._sort_order = order

        reverse = (order == Qt.DescendingOrder)

        if column == self.COL_NAME:
            self._sounds.sort(key=lambda s: s.name.lower(), reverse=reverse)
        elif column == self.COL_DURATION:
            self._sounds.sort(key=lambda s: s.duration, reverse=reverse)
        elif column == self.COL_KEY:
            self._sounds.sort(key=lambda s: s.key.lower(), reverse=reverse)
        elif column == self.COL_CATEGORY:
            self._sounds.sort(key=lambda s: s.category.lower(), reverse=reverse)
        elif column == self.COL_VOLUME:
            self._sounds.sort(key=lambda s: s.volume, reverse=reverse)
        elif column == self.COL_LAST_PLAYED:
            def last_played_key(s):
                if s.last_played:
                    try:
                        return datetime.fromisoformat(s.last_played)
                    except:
                        return datetime.min
                return datetime.min
            self._sounds.sort(key=last_played_key, reverse=reverse)
        elif column == self.COL_ADDED_DATE:
            def added_date_key(s):
                if s.added_date:
                    try:
                        return datetime.fromisoformat(s.added_date)
                    except:
                        return datetime.min
                return datetime.min
            self._sounds.sort(key=added_date_key, reverse=reverse)
        elif column == self.COL_PLAY or column == self.COL_STOP:
            # Не сортируем по кнопкам
            return
        else:
            # По умолчанию - по order (кастомный порядок)
            self._sounds.sort(key=lambda s: s.order, reverse=reverse)

        # Сохраняем отсортированный список в конфиг (если не кастомный порядок)
        if self.config.config.sort_mode != "order":
            self.config.config.sounds = self._sounds.copy()
            self.config.mark_changed()

        self.layoutChanged.emit()

    def set_custom_order(self):
        """Применить кастомный порядок (сортировка по order)."""
        self._sounds.sort(key=lambda s: s.order)
        self.layoutChanged.emit()

    def add_sound(self, sound: SoundItem):
        """Добавить новый звук."""
        # Назначаем order = max order + 1
        max_order = max((s.order for s in self._sounds), default=-1)
        sound.order = max_order + 1
        self.beginInsertRows(QModelIndex(), len(self._sounds), len(self._sounds))
        self._sounds.append(sound)
        self.config.config.sounds = self._sounds.copy()
        self.endInsertRows()
        self.config.mark_changed()

    def remove_sounds(self, rows: List[int]):
        """Удалить звуки по индексам строк."""
        if not rows:
            return
        rows = sorted(rows, reverse=True)
        for row in rows:
            self.beginRemoveRows(QModelIndex(), row, row)
            del self._sounds[row]
            self.endRemoveRows()
        self.config.config.sounds = self._sounds.copy()
        self.config.mark_changed()

    def get_sound_at(self, row: int) -> Optional[SoundItem]:
        if 0 <= row < len(self._sounds):
            return self._sounds[row]
        return None

    def moveRows(self, source_parent, source_row, count, destination_parent, destination_child):
        """Перемещение строк для drag & drop."""
        if source_parent.isValid() or destination_parent.isValid():
            return False
        if source_row < 0 or source_row + count > len(self._sounds):
            return False
        if destination_child < 0 or destination_child > len(self._sounds):
            return False

        # Перемещаем элементы в списке
        items = self._sounds[source_row:source_row+count]
        del self._sounds[source_row:source_row+count]
        if destination_child > source_row:
            destination_child -= count
        for i, item in enumerate(items):
            self._sounds.insert(destination_child + i, item)

        # Обновляем order для всех элементов
        for idx, sound in enumerate(self._sounds):
            sound.order = idx

        self.config.config.sounds = self._sounds.copy()
        self.config.mark_changed()
        self.layoutChanged.emit()
        return True

    def supportedDropActions(self):
        return Qt.MoveAction

    def supportedDragActions(self):
        return Qt.MoveAction

# ----------------------------------------------------------------------
# Делегат для кнопок Play/Stop
# ----------------------------------------------------------------------
class ButtonDelegate(QStyledItemDelegate):
    """Делегат для отрисовки кнопок в ячейках Play/Stop."""
    buttonClicked = Signal(int, int)  # row, column

    def paint(self, painter, option, index):
        if index.column() in (SoundsTableModel.COL_PLAY, SoundsTableModel.COL_STOP):
            # Рисуем кнопку
            opt = QStyleOptionButton()
            opt.rect = option.rect
            opt.text = index.data(Qt.DisplayRole)
            opt.state = QStyle.State_Enabled
            if option.state & QStyle.State_MouseOver:
                opt.state |= QStyle.State_MouseOver
            QApplication.style().drawControl(QStyle.CE_PushButton, opt, painter)
        else:
            super().paint(painter, option, index)

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.MouseButtonRelease:
            if index.column() in (SoundsTableModel.COL_PLAY, SoundsTableModel.COL_STOP):
                self.buttonClicked.emit(index.row(), index.column())
                return True
        return super().editorEvent(event, model, option, index)

# ----------------------------------------------------------------------
# Прокси-модель для фильтрации
# ----------------------------------------------------------------------
class FilterProxyModel(QSortFilterProxyModel):
    """Только фильтрация, без сортировки."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.filter_text = ""

    def setFilterText(self, text: str):
        self.filter_text = text.lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        if not self.filter_text:
            return True
        model = self.sourceModel()
        index = model.index(source_row, SoundsTableModel.COL_NAME, source_parent)
        name = model.data(index, Qt.DisplayRole).lower()
        return self.filter_text in name

    def lessThan(self, left, right):
        # Не используем сортировку в прокси
        return False

# ----------------------------------------------------------------------
# Диалог захвата комбинации клавиш (переиспользуемый)
# ----------------------------------------------------------------------
def capture_key_combination(parent=None, title="Захват клавиш") -> Optional[str]:
    """Диалог захвата комбинации клавиш, возвращает строку типа 'ctrl+shift+a'."""
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setModal(True)
    layout = QVBoxLayout(dialog)
    label = QLabel("Нажмите желаемую комбинацию...")
    layout.addWidget(label)
    key_label = QLabel("")
    key_label.setAlignment(Qt.AlignCenter)
    key_label.setStyleSheet("font-size: 16px; font-weight: bold;")
    layout.addWidget(key_label)

    pressed_keys = set()
    handler = None
    result = None

    def on_key_event(e):
        nonlocal pressed_keys, handler, result
        if e.event_type == keyboard.KEY_DOWN:
            name = e.name
            if name not in pressed_keys:
                pressed_keys.add(name)
        elif e.event_type == keyboard.KEY_UP:
            if pressed_keys:
                combo = "+".join(sorted(pressed_keys))
                key_label.setText(combo)
                result = combo
                if handler:
                    keyboard.unhook(handler)
                dialog.accept()
            pressed_keys.clear()

    handler = keyboard.hook(on_key_event)
    dialog.finished.connect(lambda: keyboard.unhook(handler) if handler else None)
    dialog.exec()
    return result

# ----------------------------------------------------------------------
# Главное окно приложения
# ----------------------------------------------------------------------
class KrimBoardMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("KrimBoard 2.0")
        self.resize(1200, 800)

        # Иконка
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "krimboard_icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        # Конфигурация
        self.config = ConfigManager()

        # Звуковой менеджер
        self.sound_manager = SoundManager(self.config)
        self.sound_manager.playback_started.connect(self.on_playback_started)
        self.sound_manager.playback_stopped.connect(self.on_playback_stopped)
        self.sound_manager.vu_level_updated.connect(self.update_vu_meter)

        # Оверлей
        self.overlay = OverlayWindow(self.config)

        # Глобальные действия для горячих клавиш
        app_actions = {
            "mute_sounds": self.toggle_mute_sounds,
            "stop_all_sounds": lambda: self.sound_manager.stop_all(fade_out=True),
            "toggle_overlay": self.toggle_overlay_visibility
        }
        self.hotkey_manager = HotkeyManager(self.config, self.sound_manager, self.overlay, app_actions, self)

        # Запускаем хук ПЕРЕД подключением сигналов
        self.hotkey_manager.start_global_hook()

        # Теперь подключаем heartbeat (поток уже должен существовать)
        if self.hotkey_manager.hook_thread is not None:
            self.hotkey_manager.hook_thread.heartbeat.connect(self.on_hook_heartbeat)
        else:
            logger.warning("Hook thread не был создан (возможно, глобальные горячие клавиши отключены)")

        # Модель и представление
        self.model = SoundsTableModel(self.config)
        self.proxy_model = FilterProxyModel()
        self.proxy_model.setSourceModel(self.model)

        self.table = QTableView()
        self.table.setModel(self.proxy_model)
        self.table.setSortingEnabled(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(SoundsTableModel.COL_NAME, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(SoundsTableModel.COL_DURATION, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(SoundsTableModel.COL_KEY, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(SoundsTableModel.COL_CATEGORY, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(SoundsTableModel.COL_VOLUME, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(SoundsTableModel.COL_LAST_PLAYED, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(SoundsTableModel.COL_PLAY, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(SoundsTableModel.COL_STOP, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(SoundsTableModel.COL_ADDED_DATE, QHeaderView.ResizeToContents)
        self.table.setColumnWidth(SoundsTableModel.COL_PLAY, 40)
        self.table.setColumnWidth(SoundsTableModel.COL_STOP, 40)

        # Делегат для кнопок
        self.button_delegate = ButtonDelegate(self.table)
        self.button_delegate.buttonClicked.connect(self.on_button_clicked)
        self.table.setItemDelegateForColumn(SoundsTableModel.COL_PLAY, self.button_delegate)
        self.table.setItemDelegateForColumn(SoundsTableModel.COL_STOP, self.button_delegate)

        # Drag & Drop (только в кастомном режиме)
        self.table.setDragEnabled(True)
        self.table.setAcceptDrops(True)
        self.table.setDragDropMode(QAbstractItemView.InternalMove)
        self.table.setDefaultDropAction(Qt.MoveAction)
        self.table.setDropIndicatorShown(True)

        # Сортировка по заголовку
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().sortIndicatorChanged.connect(self.on_header_sort)

        # Переменные
        self._saved_volume = self.config.config.sound_volume
        self.filter_timer = QTimer()
        self.filter_timer.setSingleShot(True)
        self.filter_timer.timeout.connect(self.apply_filter)

        # Мониторинг здоровья хука
        self.last_heartbeat_time = time.time()
        self.health_check_timer = QTimer()
        self.health_check_timer.timeout.connect(self.check_hook_health)
        self.health_check_timer.start(HEALTH_CHECK_INTERVAL_MS)

        # Оверлей предупреждения о засыпании
        self.sleep_overlay = SleepNotificationOverlay()
        self.sleep_overlay.restart_requested.connect(self.restart_application)

        # Построение интерфейса
        self.setup_ui()
        self.apply_theme()

        # Восстановление сортировки
        self.restore_sort_mode()

        # Таймер для обновления VU
        self.vu_timer = QTimer()
        self.vu_timer.timeout.connect(self.update_vu_display)
        self.vu_timer.start(VU_UPDATE_INTERVAL_MS)

        # Трей
        self.create_tray_icon()

        # Предупреждение о FFmpeg
        if FFMPEG_PATH is None:
            QMessageBox.warning(
                self,
                "FFmpeg не найден",
                "Для воспроизведения MP3, OGG и других форматов (кроме WAV) требуется ffmpeg.\n"
                "Скачайте с ffmpeg.org и положите ffmpeg.exe в папку с программой или добавьте в PATH.\n\n"
                "WAV-файлы будут работать без проблем."
            )

    def setup_ui(self):
        # Центральный виджет
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(5, 5, 5, 5)
        main_layout.setSpacing(5)

        # Тулбар
        self.create_toolbar()

        # Панель поиска и сортировки
        top_layout = QHBoxLayout()
        filter_label = QLabel("Поиск:")
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Введите название...")
        self.filter_edit.textChanged.connect(self.on_filter_text_changed)
        top_layout.addWidget(filter_label)
        top_layout.addWidget(self.filter_edit)

        sort_label = QLabel("Сортировка:")
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Кастомный порядок", "По алфавиту", "По длительности", "По клавише", "По категории", "По громкости", "По дате воспр.", "По дате добавл."])
        self.sort_combo.currentIndexChanged.connect(self.on_sort_mode_changed)
        top_layout.addWidget(sort_label)
        top_layout.addWidget(self.sort_combo)
        main_layout.addLayout(top_layout)

        # Таблица
        main_layout.addWidget(self.table)

        # VU-метр
        vu_layout = QHBoxLayout()
        vu_layout.addWidget(QLabel("Уровень звука:"))
        self.vu_bar = QProgressBar()
        self.vu_bar.setRange(0, 100)
        self.vu_bar.setTextVisible(False)
        self.vu_bar.setStyleSheet("QProgressBar::chunk { background-color: green; }")
        vu_layout.addWidget(self.vu_bar)
        main_layout.addLayout(vu_layout)

        # Статус-бар
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_label = QLabel("Готов")
        self.status_bar.addWidget(self.status_label)

    def create_toolbar(self):
        toolbar = QToolBar("Основные действия")
        self.addToolBar(toolbar)

        add_file_action = QAction("Добавить файл", self)
        add_file_action.triggered.connect(self.add_sound)
        toolbar.addAction(add_file_action)

        add_folder_action = QAction("Добавить папку", self)
        add_folder_action.triggered.connect(self.add_folder)
        toolbar.addAction(add_folder_action)

        remove_action = QAction("Удалить", self)
        remove_action.triggered.connect(self.remove_sound)
        toolbar.addAction(remove_action)

        assign_key_action = QAction("Назначить клавишу", self)
        assign_key_action.triggered.connect(self.assign_key)
        toolbar.addAction(assign_key_action)

        settings_action = QAction("Настройки", self)
        settings_action.triggered.connect(self.open_settings_dialog)
        toolbar.addAction(settings_action)

        save_action = QAction("Сохранить", self)
        save_action.triggered.connect(self.manual_save)
        toolbar.addAction(save_action)

        stop_all_action = QAction("Остановить все", self)
        stop_all_action.triggered.connect(lambda: self.sound_manager.stop_all(fade_out=True))
        toolbar.addAction(stop_all_action)

        reset_keys_action = QAction("♻️ Reanimate Hook", self)
        reset_keys_action.triggered.connect(self.hotkey_manager.restart_hotkeys)
        toolbar.addAction(reset_keys_action)

        restart_app_action = QAction("🗘 Перезапустить приложение", self)
        restart_app_action.triggered.connect(self.restart_application)
        toolbar.addAction(restart_app_action)

        import_action = QAction("Импорт конфига", self)
        import_action.triggered.connect(self.import_config_dialog)
        toolbar.addAction(import_action)

        export_action = QAction("Экспорт конфига", self)
        export_action.triggered.connect(self.export_config_dialog)
        toolbar.addAction(export_action)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        toolbar.addWidget(spacer)

        theme_action = QAction("Сменить тему", self)
        theme_action.triggered.connect(self.toggle_theme)
        toolbar.addAction(theme_action)

    def create_tray_icon(self):
        """Создание иконки в трее."""
        self.tray_icon = QSystemTrayIcon(self)
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "krimboard_icon.png")
        if os.path.exists(icon_path):
            self.tray_icon.setIcon(QIcon(icon_path))
        else:
            self.tray_icon.setIcon(QIcon.fromTheme("audio-x-generic"))

        tray_menu = QMenu()
        show_action = tray_menu.addAction("Показать")
        show_action.triggered.connect(self.show_normal)
        hide_action = tray_menu.addAction("Свернуть в трей")
        hide_action.triggered.connect(self.hide_to_tray)
        tray_menu.addSeparator()
        restart_hook_action = tray_menu.addAction("♻️ Reanimate Hook")
        restart_hook_action.triggered.connect(self.hotkey_manager.restart_hotkeys)
        restart_app_action = tray_menu.addAction("🗘 Перезапустить приложение")
        restart_app_action.triggered.connect(self.restart_application)
        tray_menu.addSeparator()
        quit_action = tray_menu.addAction("Выход")
        quit_action.triggered.connect(self.quit_app)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_normal()

    def show_normal(self):
        self.show()
        self.activateWindow()

    def hide_to_tray(self):
        self.hide()
        self.tray_icon.showMessage("KrimBoard", "Приложение свёрнуто в трей", QSystemTrayIcon.Information, 2000)

    def quit_app(self):
        self.hotkey_manager.stop_global_hook()
        self.tray_icon.hide()
        QApplication.quit()

    def closeEvent(self, event):
        event.ignore()
        self.hide_to_tray()

    # ------------------------------------------------------------------
    # Перезапуск приложения
    # ------------------------------------------------------------------
    def restart_application(self):
        """Полный перезапуск приложения."""
        logger.info("Перезапуск приложения...")
        # Сохраняем конфиг перед перезапуском
        self.config.save()
        # Запускаем новый процесс с флагом --minimized
        try:
            if getattr(sys, 'frozen', False):
                # Если запущено как exe
                executable = sys.executable
                args = [executable, "--minimized"]
            else:
                # Запуск через интерпретатор Python
                executable = sys.executable
                script = os.path.abspath(__file__)
                args = [executable, script, "--minimized"]
            subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
            # Завершаем текущий процесс
            QApplication.quit()
        except Exception as e:
            logger.error(f"Не удалось перезапустить приложение: {e}")
            QMessageBox.critical(self, "Ошибка", f"Не удалось перезапустить приложение:\n{e}")

    # ------------------------------------------------------------------
    # Мониторинг здоровья хука
    # ------------------------------------------------------------------
    @Slot()
    def on_hook_heartbeat(self):
        """Получен heartbeat от хука."""
        self.last_heartbeat_time = time.time()

    def check_hook_health(self):
        """Проверяем, не заснул ли хук."""
        if not self.config.config.enable_global_hotkeys:
            return
        elapsed = time.time() - self.last_heartbeat_time
        if elapsed > HEARTBEAT_TIMEOUT_SEC:
            logger.warning(f"Хук не подавал признаков жизни {elapsed:.1f} секунд. Возможно, он заснул.")
            self.on_hook_sleep_detected()

    def on_hook_sleep_detected(self):
        """Действия при обнаружении засыпания хука."""
        # Звуковое оповещение
        if self.config.config.attention_sound_enabled:
            self.play_attention_sound()
        # Показать оверлей, если разрешено
        if self.config.config.sleep_overlay_enabled:
            self.sleep_overlay.show_overlay()
        # Обновить статус
        self.status_label.setText("⚠️ Глобальные клавиши не работают! Нажмите 'Reanimate Hook' или перезапустите приложение.")
        self.status_label.setStyleSheet("color: red; font-weight: bold;")

    def play_attention_sound(self):
        """Воспроизвести звук внимания (attention.mp3 или системный beep)."""
        if not self.config.config.attention_sound_enabled:
            return

        attention_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "attention.mp3")
        try:
            if os.path.exists(attention_path):
                # Простой способ без создания SoundItem (меньше overhead)
                if FFMPEG_PATH and FFMPEG_PATH != "ffmpeg":
                    AudioSegment.converter = FFMPEG_PATH
                audio = AudioSegment.from_file(attention_path)[:1500]  # максимум 1.5 секунды
                samples = np.array(audio.get_array_of_samples()).astype(np.float32)
                if audio.channels == 2:
                    samples = samples.reshape((-1, 2))
                samples /= np.iinfo(samples.dtype).max

                devices = []
                if self.config.config.primary_device:
                    devices.append(self.config.config.primary_device.index)
                if self.config.config.secondary_device:
                    devices.append(self.config.config.secondary_device.index)

                for dev in devices:
                    sd.play(samples, audio.frame_rate, device=dev)
                    sd.wait()  # ждём окончания короткого сигнала
            else:
                QApplication.beep()
        except Exception as e:
            logger.error(f"Не удалось воспроизвести attention.mp3: {e}")
            QApplication.beep()

    def reconnect_heartbeat(self):
        """Переподключает сигнал heartbeat от хука к главному окну."""
        if self.hotkey_manager.hook_thread is not None:
            try:
                self.hotkey_manager.hook_thread.heartbeat.disconnect(self.on_hook_heartbeat)
            except:
                pass
            self.hotkey_manager.hook_thread.heartbeat.connect(self.on_hook_heartbeat)

    # ------------------------------------------------------------------
    # Работа со звуками
    # ------------------------------------------------------------------
    def add_sound(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Выберите аудиофайл", "",
            "Аудио (*.wav *.mp3 *.ogg *.flac *.m4a *.aac);;Все файлы (*.*)"
        )
        if not file_path:
            return
        file_path = os.path.normpath(file_path)
        name = os.path.basename(file_path)
        duration = self._get_duration(file_path)
        sound = SoundItem(name=name, file=file_path, duration=duration, added_date=datetime.now().isoformat())
        self.model.add_sound(sound)
        self.hotkey_manager.setup_sound_hotkeys()

    def add_folder(self):
        folder_path = QFileDialog.getExistingDirectory(self, "Выберите папку с аудио")
        if not folder_path:
            return
        folder_path = os.path.normpath(folder_path)
        added = 0
        now = datetime.now().isoformat()
        for filename in os.listdir(folder_path):
            if filename.lower().endswith(AUDIO_EXTENSIONS):
                full_path = os.path.normpath(os.path.join(folder_path, filename))
                if not any(s.file == full_path for s in self.config.config.sounds):
                    duration = self._get_duration(full_path)
                    sound = SoundItem(name=filename, file=full_path, duration=duration, added_date=now)
                    self.model.add_sound(sound)
                    added += 1
        if added > 0:
            self.hotkey_manager.setup_sound_hotkeys()
            QMessageBox.information(self, "Информация", f"Добавлено {added} файлов.")
        else:
            QMessageBox.information(self, "Информация", "Новых аудиофайлов не найдено.")

    def _get_duration(self, file_path: str) -> float:
        try:
            if FFMPEG_PATH and FFMPEG_PATH != "ffmpeg":
                AudioSegment.converter = FFMPEG_PATH
            audio = AudioSegment.from_file(file_path)
            return len(audio) / 1000.0
        except Exception as e:
            logger.error(f"Не удалось получить длительность {file_path}: {e}")
            return 0.0

    def remove_sound(self):
        selection_model = self.table.selectionModel()
        if not selection_model.hasSelection():
            return
        rows = set()
        for index in selection_model.selectedRows():
            source_index = self.proxy_model.mapToSource(index)
            rows.add(source_index.row())
        if not rows:
            return

        count = len(rows)
        reply = QMessageBox.question(
            self, "Подтверждение удаления",
            f"Удалить {count} звук(ов)?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        self.model.remove_sounds(list(rows))
        self.hotkey_manager.setup_sound_hotkeys()

    def assign_key(self):
        selection = self.table.selectionModel().selectedRows()
        if len(selection) != 1:
            QMessageBox.warning(self, "Предупреждение", "Выберите один звук для назначения клавиши")
            return
        source_index = self.proxy_model.mapToSource(selection[0])
        row = source_index.row()
        sound = self.model.get_sound_at(row)
        if not sound:
            return

        combo = capture_key_combination(self, "Назначение клавиши")
        if combo:
            sound.key = combo
            self.model.dataChanged.emit(source_index, source_index, [Qt.EditRole])
            self.config.mark_changed()
            self.hotkey_manager.setup_sound_hotkeys()

    def on_button_clicked(self, row: int, col: int):
        """Обработка клика по кнопкам Play/Stop."""
        source_index = self.model.index(row, 0)
        sound = self.model.get_sound_at(row)
        if not sound:
            return

        if col == SoundsTableModel.COL_PLAY:
            self.sound_manager.play(sound)
        elif col == SoundsTableModel.COL_STOP:
            self.sound_manager.stop_sound(sound.name, fade_out=True)

    # ------------------------------------------------------------------
    # Сортировка
    # ------------------------------------------------------------------
    def restore_sort_mode(self):
        mode = self.config.config.sort_mode
        order = self.config.config.sort_order
        index_map = {
            "order": 0,
            "name": 1,
            "duration": 2,
            "key": 3,
            "category": 4,
            "volume": 5,
            "last_played": 6,
            "added_date": 7
        }
        idx = index_map.get(mode, 0)
        self.sort_combo.setCurrentIndex(idx)
        self.apply_sort_mode(mode, order)

    def on_sort_mode_changed(self, idx: int):
        mode_map = {
            0: "order",
            1: "name",
            2: "duration",
            3: "key",
            4: "category",
            5: "volume",
            6: "last_played",
            7: "added_date"
        }
        mode = mode_map.get(idx, "order")
        order = self.config.config.sort_order
        self.apply_sort_mode(mode, order)

        if mode == "order":
            self.table.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)
        else:
            col_map = {
                "name": SoundsTableModel.COL_NAME,
                "duration": SoundsTableModel.COL_DURATION,
                "key": SoundsTableModel.COL_KEY,
                "category": SoundsTableModel.COL_CATEGORY,
                "volume": SoundsTableModel.COL_VOLUME,
                "last_played": SoundsTableModel.COL_LAST_PLAYED,
                "added_date": SoundsTableModel.COL_ADDED_DATE
            }
            col = col_map.get(mode, 0)
            self.table.horizontalHeader().setSortIndicator(col, order)

    def on_header_sort(self, logical_index: int, order: Qt.SortOrder):
        col_map = {
            SoundsTableModel.COL_NAME: "name",
            SoundsTableModel.COL_DURATION: "duration",
            SoundsTableModel.COL_KEY: "key",
            SoundsTableModel.COL_CATEGORY: "category",
            SoundsTableModel.COL_VOLUME: "volume",
            SoundsTableModel.COL_LAST_PLAYED: "last_played",
            SoundsTableModel.COL_ADDED_DATE: "added_date"
        }
        mode = col_map.get(logical_index)
        if mode:
            self.config.config.sort_mode = mode
            self.config.config.sort_order = order
            self.apply_sort_mode(mode, order)
            idx_map = {"name":1, "duration":2, "key":3, "category":4, "volume":5, "last_played":6, "added_date":7}
            self.sort_combo.blockSignals(True)
            self.sort_combo.setCurrentIndex(idx_map.get(mode, 0))
            self.sort_combo.blockSignals(False)
        else:
            pass

    def apply_sort_mode(self, mode: str, order: Qt.SortOrder):
        if mode == "order":
            self.model.set_custom_order()
            self.table.setDragEnabled(True)
        else:
            col = {
                "name": SoundsTableModel.COL_NAME,
                "duration": SoundsTableModel.COL_DURATION,
                "key": SoundsTableModel.COL_KEY,
                "category": SoundsTableModel.COL_CATEGORY,
                "volume": SoundsTableModel.COL_VOLUME,
                "last_played": SoundsTableModel.COL_LAST_PLAYED,
                "added_date": SoundsTableModel.COL_ADDED_DATE
            }.get(mode, 0)
            self.model.sort(col, order)
            self.table.setDragEnabled(False)

        self.config.config.sort_mode = mode
        self.config.config.sort_order = order
        self.config.mark_changed()

    # ------------------------------------------------------------------
    # Фильтрация
    # ------------------------------------------------------------------
    def on_filter_text_changed(self, text: str):
        self.filter_timer.start(200)

    def apply_filter(self):
        text = self.filter_edit.text()
        self.proxy_model.setFilterText(text)

    # ------------------------------------------------------------------
    # Воспроизведение и оверлей
    # ------------------------------------------------------------------
    def on_playback_started(self, name: str):
        self.status_label.setText(f"▶ Воспроизведение: {name}")
        self.status_label.setStyleSheet("")  # сброс цвета
        for row in range(self.proxy_model.rowCount()):
            idx = self.proxy_model.index(row, 0)
            if idx.data(Qt.DisplayRole) == name:
                self.table.selectRow(row)
                break
        self.overlay.set_text(name)
        self.overlay.show_overlay()

    def on_playback_stopped(self):
        self.status_label.setText("Готов")
        self.overlay.hide()
        self.overlay.hide_timer.stop()

    def toggle_overlay_visibility(self):
        if self.overlay.isVisible():
            self.overlay.hide()
        else:
            self.overlay.show_overlay()

    # ------------------------------------------------------------------
    # VU-метр
    # ------------------------------------------------------------------
    def update_vu_meter(self, level: float):
        self._vu_level = level

    def update_vu_display(self):
        if hasattr(self, '_vu_level'):
            value = int(self._vu_level * 100)
            self.vu_bar.setValue(min(value, 100))
            if value > 80:
                self.vu_bar.setStyleSheet("QProgressBar::chunk { background-color: red; }")
            elif value > 50:
                self.vu_bar.setStyleSheet("QProgressBar::chunk { background-color: yellow; }")
            else:
                self.vu_bar.setStyleSheet("QProgressBar::chunk { background-color: green; }")

    # ------------------------------------------------------------------
    # Настройки
    # ------------------------------------------------------------------
    def open_settings_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Настройки")
        dialog.resize(700, 600)
        layout = QVBoxLayout(dialog)

        tab_widget = QTabWidget()
        layout.addWidget(tab_widget)

        devices_tab = QWidget()
        tab_widget.addTab(devices_tab, "Устройства")
        self.setup_devices_tab(devices_tab)

        volume_tab = QWidget()
        tab_widget.addTab(volume_tab, "Громкость")
        self.setup_volume_tab(volume_tab)

        overlay_tab = QWidget()
        tab_widget.addTab(overlay_tab, "Оверлей")
        self.setup_overlay_tab(overlay_tab)

        hotkeys_tab = QWidget()
        tab_widget.addTab(hotkeys_tab, "Горячие клавиши")
        self.setup_hotkeys_tab(hotkeys_tab)

        misc_tab = QWidget()
        tab_widget.addTab(misc_tab, "Прочее")
        self.setup_misc_tab(misc_tab)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(lambda: self.apply_settings(dialog))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        dialog.exec()

    def get_output_devices_info(self):
        devices = sd.query_devices()
        out_devs = []
        for i, dev in enumerate(devices):
            if dev['max_output_channels'] > 0:
                hostapi = sd.query_hostapis(dev['hostapi'])['name']
                out_devs.append({
                    "index": i,
                    "name": dev['name'],
                    "hostapi": hostapi,
                    "display": f"{i}: {dev['name']} ({hostapi})"
                })
        return out_devs

    def setup_devices_tab(self, tab):
        layout = QVBoxLayout(tab)
        devices_info = self.get_output_devices_info()
        displays = [d["display"] for d in devices_info]

        primary_group = QGroupBox("Основное устройство")
        primary_layout = QVBoxLayout(primary_group)
        self.primary_combo = QComboBox()
        self.primary_combo.addItems(displays)
        if self.config.config.primary_device:
            for i, d in enumerate(devices_info):
                if d["index"] == self.config.config.primary_device.index:
                    self.primary_combo.setCurrentIndex(i)
                    break
        primary_layout.addWidget(self.primary_combo)
        layout.addWidget(primary_group)

        secondary_group = QGroupBox("Дополнительное устройство")
        secondary_layout = QVBoxLayout(secondary_group)
        self.secondary_combo = QComboBox()
        self.secondary_combo.addItem("(не выбрано)")
        self.secondary_combo.addItems(displays)
        if self.config.config.secondary_device:
            for i, d in enumerate(devices_info):
                if d["index"] == self.config.config.secondary_device.index:
                    self.secondary_combo.setCurrentIndex(i + 1)
                    break
        secondary_layout.addWidget(self.secondary_combo)
        layout.addWidget(secondary_group)

        test_btn = QPushButton("Тест основного устройства (440 Гц)")
        test_btn.clicked.connect(lambda: self.test_device(devices_info))
        layout.addWidget(test_btn)
        layout.addStretch()

    def test_device(self, devices_info):
        idx = self.primary_combo.currentIndex()
        if idx < 0:
            QMessageBox.warning(self, "Ошибка", "Выберите устройство")
            return
        dev_index = devices_info[idx]["index"]
        duration = 1.0
        samplerate = 44100
        t = np.linspace(0, duration, int(samplerate * duration), endpoint=False)
        tone = 0.3 * np.sin(2 * np.pi * 440 * t)
        if tone.ndim == 1:
            tone = np.column_stack((tone, tone))
        sd.play(tone, samplerate, device=dev_index)
        QMessageBox.information(self, "Тест", "Воспроизводится тестовый сигнал 440 Гц.")

    def setup_volume_tab(self, tab):
        layout = QVBoxLayout(tab)
        vol_layout = QFormLayout()
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(self.config.config.sound_volume)
        self.volume_label = QLabel(f"{self.config.config.sound_volume}%")
        self.volume_slider.valueChanged.connect(lambda v: self.volume_label.setText(f"{v}%"))
        vol_layout.addRow("Громкость звуков:", self.volume_slider)
        vol_layout.addRow("", self.volume_label)
        layout.addLayout(vol_layout)
        layout.addStretch()

    def setup_overlay_tab(self, tab):
        layout = QVBoxLayout(tab)
        form = QFormLayout()

        self.overlay_enabled_cb = QCheckBox()
        self.overlay_enabled_cb.setChecked(self.config.config.overlay_enabled)
        form.addRow("Включить оверлей:", self.overlay_enabled_cb)

        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(10, 100)
        self.opacity_slider.setValue(int(self.config.config.overlay_opacity * 100))
        self.opacity_label = QLabel(f"{int(self.config.config.overlay_opacity * 100)}%")
        self.opacity_slider.valueChanged.connect(lambda v: self.opacity_label.setText(f"{v}%"))
        form.addRow("Прозрачность:", self.opacity_slider)
        form.addRow("", self.opacity_label)

        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(8, 72)
        self.font_size_spin.setValue(self.config.config.overlay_font_size)
        form.addRow("Размер шрифта:", self.font_size_spin)

        self.position_combo = QComboBox()
        self.position_combo.addItems(["top-left", "top-right", "bottom-left", "bottom-right", "custom"])
        self.position_combo.setCurrentText(self.config.config.overlay_position)
        form.addRow("Положение:", self.position_combo)

        custom_widget = QWidget()
        custom_layout = QHBoxLayout(custom_widget)
        self.custom_x_spin = QSpinBox()
        self.custom_x_spin.setRange(0, 9999)
        self.custom_x_spin.setValue(self.config.config.overlay_custom_x)
        self.custom_y_spin = QSpinBox()
        self.custom_y_spin.setRange(0, 9999)
        self.custom_y_spin.setValue(self.config.config.overlay_custom_y)
        custom_layout.addWidget(QLabel("X:"))
        custom_layout.addWidget(self.custom_x_spin)
        custom_layout.addWidget(QLabel("Y:"))
        custom_layout.addWidget(self.custom_y_spin)
        form.addRow("Кастомные координаты:", custom_widget)

        timeout_layout = QHBoxLayout()
        self.timeout_slider = QSlider(Qt.Horizontal)
        self.timeout_slider.setRange(0, 300)
        self.timeout_slider.setValue(int(self.config.config.overlay_timeout * 10))
        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(0.0, 30.0)
        self.timeout_spin.setSingleStep(0.1)
        self.timeout_spin.setValue(self.config.config.overlay_timeout)
        self.timeout_spin.setDecimals(1)
        self.timeout_label = QLabel(f"{self.config.config.overlay_timeout:.1f} с")

        self.timeout_slider.valueChanged.connect(lambda v: self.timeout_spin.setValue(v / 10.0))
        self.timeout_spin.valueChanged.connect(lambda v: self.timeout_slider.setValue(int(v * 10)))
        self.timeout_spin.valueChanged.connect(lambda v: self.timeout_label.setText(f"{v:.1f} с"))

        timeout_layout.addWidget(self.timeout_slider)
        timeout_layout.addWidget(self.timeout_spin)
        timeout_layout.addWidget(self.timeout_label)
        form.addRow("Время отображения (сек):", timeout_layout)

        layout.addLayout(form)
        layout.addStretch()

    def setup_hotkeys_tab(self, tab):
        layout = QVBoxLayout(tab)
        self.hotkey_widgets = {}
        actions = [
            ("mute_sounds", "Выключить звук (mute)"),
            ("stop_all_sounds", "Остановить все звуки"),
            ("toggle_overlay", "Показать/скрыть оверлей")
        ]
        form = QFormLayout()
        for action, label in actions:
            h_layout = QHBoxLayout()
            edit = QLineEdit()
            edit.setText(self.config.config.global_hotkeys.get(action, ""))
            edit.setReadOnly(True)
            btn = QPushButton("Назначить")
            btn.clicked.connect(lambda checked, a=action, e=edit: self.capture_global_hotkey(a, e))
            h_layout.addWidget(edit)
            h_layout.addWidget(btn)
            form.addRow(label + ":", h_layout)
            self.hotkey_widgets[action] = edit
        layout.addLayout(form)
        layout.addStretch()

    def capture_global_hotkey(self, action: str, edit: QLineEdit):
        combo = capture_key_combination(self, f"Назначение клавиши для {action}")
        if combo:
            edit.setText(combo)

    def setup_misc_tab(self, tab):
        layout = QVBoxLayout(tab)
        self.auto_save_cb = QCheckBox("Автосохранение каждые 5 минут")
        self.auto_save_cb.setChecked(self.config.config.auto_save_enabled)
        layout.addWidget(self.auto_save_cb)

        self.logging_cb = QCheckBox("Вести лог воспроизведений (krimboard_log.json)")
        self.logging_cb.setChecked(self.config.config.logging_enabled)
        layout.addWidget(self.logging_cb)

        self.enable_global_cb = QCheckBox("Использовать глобальные горячие клавиши")
        self.enable_global_cb.setChecked(self.config.config.enable_global_hotkeys)
        layout.addWidget(self.enable_global_cb)

        # Новые настройки для защиты от засыпания
        self.sleep_overlay_cb = QCheckBox("Показывать оверлей при засыпании хука")
        self.sleep_overlay_cb.setChecked(self.config.config.sleep_overlay_enabled)
        layout.addWidget(self.sleep_overlay_cb)

        self.attention_sound_cb = QCheckBox("Воспроизводить звук внимания при засыпании")
        self.attention_sound_cb.setChecked(self.config.config.attention_sound_enabled)
        layout.addWidget(self.attention_sound_cb)

        form = QFormLayout()
        self.ignored_edit = QLineEdit()
        self.ignored_edit.setText(", ".join(sorted(self.config.config.ignored_keys)))
        form.addRow("Игнорируемые клавиши (через запятую):", self.ignored_edit)
        layout.addLayout(form)

        theme_layout = QHBoxLayout()
        theme_layout.addWidget(QLabel("Тема:"))
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["light", "dark"])
        self.theme_combo.setCurrentText(self.config.config.theme)
        theme_layout.addWidget(self.theme_combo)
        layout.addLayout(theme_layout)

        layout.addStretch()

    def apply_settings(self, dialog):
        # Устройства
        devices_info = self.get_output_devices_info()
        primary_idx = self.primary_combo.currentIndex()
        if primary_idx >= 0:
            d = devices_info[primary_idx]
            self.config.config.primary_device = OutputDevice(index=d["index"], name=d["name"], hostapi=d["hostapi"])
        else:
            self.config.config.primary_device = None

        secondary_idx = self.secondary_combo.currentIndex()
        if secondary_idx > 0:
            d = devices_info[secondary_idx - 1]
            self.config.config.secondary_device = OutputDevice(index=d["index"], name=d["name"], hostapi=d["hostapi"])
        else:
            self.config.config.secondary_device = None

        # Громкость
        self.config.config.sound_volume = self.volume_slider.value()

        # Оверлей
        self.config.config.overlay_enabled = self.overlay_enabled_cb.isChecked()
        self.config.config.overlay_opacity = self.opacity_slider.value() / 100.0
        self.config.config.overlay_font_size = self.font_size_spin.value()
        self.config.config.overlay_position = self.position_combo.currentText()
        self.config.config.overlay_custom_x = self.custom_x_spin.value()
        self.config.config.overlay_custom_y = self.custom_y_spin.value()
        self.config.config.overlay_timeout = self.timeout_spin.value()

        # Горячие клавиши
        new_hotkeys = {}
        for action, edit in self.hotkey_widgets.items():
            combo = edit.text().strip()
            if combo:
                new_hotkeys[action] = combo
        self.config.config.global_hotkeys = new_hotkeys

        # Прочее
        self.config.config.auto_save_enabled = self.auto_save_cb.isChecked()
        self.config.config.logging_enabled = self.logging_cb.isChecked()
        old_enable = self.config.config.enable_global_hotkeys
        self.config.config.enable_global_hotkeys = self.enable_global_cb.isChecked()
        self.config.config.sleep_overlay_enabled = self.sleep_overlay_cb.isChecked()
        self.config.config.attention_sound_enabled = self.attention_sound_cb.isChecked()

        ignored_text = self.ignored_edit.text()
        ignored_list = [k.strip().lower() for k in ignored_text.split(',') if k.strip()]
        self.config.config.ignored_keys = set(ignored_list)
        new_theme = self.theme_combo.currentText()
        if new_theme != self.config.config.theme:
            self.config.config.theme = new_theme
            self.apply_theme()

        self.config.save()

        # Обновление компонентов
        self.overlay.set_opacity(self.config.config.overlay_opacity)
        self.overlay.update_font()
        self.overlay.update_position()

        if self.config.config.enable_global_hotkeys != old_enable:
            if self.config.config.enable_global_hotkeys:
                self.hotkey_manager.start_global_hook()
            else:
                self.hotkey_manager.stop_global_hook()
        else:
            self.hotkey_manager.setup_global_hotkeys()
        self.hotkey_manager.setup_sound_hotkeys()

        dialog.accept()
        logger.info("Настройки применены.")

    # ------------------------------------------------------------------
    # Глобальные действия
    # ------------------------------------------------------------------
    def toggle_mute_sounds(self):
        if self.config.config.sound_volume > 0:
            self._saved_volume = self.config.config.sound_volume
            self.config.config.sound_volume = 0
        else:
            self.config.config.sound_volume = self._saved_volume
        self.config.mark_changed()
        logger.info(f"Громкость звуков изменена на {self.config.config.sound_volume}")

    def manual_save(self):
        self.config.save()
        QMessageBox.information(self, "Информация", "Конфигурация сохранена")

    def import_config_dialog(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Импорт конфигурации", "", "JSON (*.json)")
        if file_path:
            if self.config.import_config(file_path):
                self.model._sounds = self.config.config.sounds.copy()
                self.model.layoutChanged.emit()
                self.hotkey_manager.setup_sound_hotkeys()
                QMessageBox.information(self, "Успех", "Конфигурация импортирована")
            else:
                QMessageBox.critical(self, "Ошибка", "Не удалось импортировать конфигурацию")

    def export_config_dialog(self):
        file_path, _ = QFileDialog.getSaveFileName(self, "Экспорт конфигурации", "", "JSON (*.json)")
        if file_path:
            if self.config.export_config(file_path):
                QMessageBox.information(self, "Успех", "Конфигурация экспортирована")
            else:
                QMessageBox.critical(self, "Ошибка", "Не удалось экспортировать конфигурацию")

    def toggle_theme(self):
        new_theme = "light" if self.config.config.theme == "dark" else "dark"
        self.config.config.theme = new_theme
        self.apply_theme()
        self.config.mark_changed()

    def apply_theme(self):
        if self.config.config.theme == "dark":
            dark_qss = """
            QMainWindow { background-color: #2b2b2b; color: #ffffff; }
            QTableView { background-color: #3c3c3c; alternate-background-color: #454545; gridline-color: #555555; color: #ffffff; }
            QHeaderView::section { background-color: #404040; color: #ffffff; border: 1px solid #555555; }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background-color: #3c3c3c; color: #ffffff; border: 1px solid #555555; }
            QPushButton { background-color: #505050; color: #ffffff; border: 1px solid #606060; padding: 5px; }
            QPushButton:hover { background-color: #606060; }
            QProgressBar { border: 1px solid #555555; background-color: #3c3c3c; text-align: center; }
            QProgressBar::chunk { background-color: #3daee9; }
            QStatusBar { background-color: #2b2b2b; color: #ffffff; }
            QTabWidget::pane { border: 1px solid #444444; background-color: #3c3c3c; }
            QTabBar::tab { background-color: #404040; color: #ffffff; padding: 6px; }
            QTabBar::tab:selected { background-color: #505050; }
            QGroupBox { color: #ffffff; border: 1px solid #555555; margin-top: 10px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
            QToolBar { background-color: #313131; border: none; }
            QToolButton { color: #ffffff; }
            QMenu { background-color: #3c3c3c; color: #ffffff; border: 1px solid #555555; }
            QMenu::item:selected { background-color: #505050; }
            """
            self.setStyleSheet(dark_qss)
        else:
            self.setStyleSheet("")

# ----------------------------------------------------------------------
# Запуск приложения
# ----------------------------------------------------------------------
if __name__ == "__main__":
    # Обработка аргументов командной строки
    start_minimized = "--minimized" in sys.argv

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)
    window = KrimBoardMainWindow()
    if start_minimized:
        window.hide_to_tray()
    else:
        window.show()
    sys.exit(app.exec())
