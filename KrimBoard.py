import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import json
import os
import sounddevice as sd
import numpy as np
from pydub import AudioSegment
import keyboard
import sv_ttk
import time
import threading
import subprocess
from datetime import datetime
import queue
import weakref

CONFIG_FILE = "krimboard_config.json"
LOG_FILE = "krimboard_log.json"
sounds = []                # [{"name":..., "file":..., "key":...}, ...]
primary_device = None      # словарь: {"index": int, "name": str, "hostapi": str} или None
secondary_device = None    # аналогично
auto_save_enabled = True
need_save = False
last_change_time = 0
auto_save_timer = None

# Глобальные переменные для VU-метра
vu_level = 0.0
vu_active = False

# --- Логирование ---
logging_enabled = False
log_lock = threading.Lock()

# --- Горячие клавиши (новый механизм) ---
hotkey_actions = {}          # ключ: frozenset(keys) -> {"file": path, "name": name, "combo_str": original_string}
hotkey_lock = threading.Lock()
current_keys = set()         # множество нажатых в данный момент клавиш
last_triggered_combo = None  # frozenset последней сработавшей комбинации
hook_handler = None          # ссылка на установленный hook

# --- Игнорируемые клавиши ---
ignored_keys = set()

# --- Настройки громкости ---
sound_volume = 100           # 0-100
microphone_volume = 100      # 0-200

# --- Микрофон ---
microphone_enabled = False
microphone_device_index = None
microphone_hotkey = ""
microphone_mode = "toggle"   # "toggle" или "push"
microphone_active = False    # текущее состояние (включен ли)
microphone_thread = None
microphone_stop_event = threading.Event()
microphone_controller = None  # будет объект MicrophoneController
microphone_hotkey_handler = None

# Путь к ffmpeg
def find_ffmpeg():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    ffmpeg_local = os.path.join(script_dir, "ffmpeg.exe")
    if os.path.exists(ffmpeg_local):
        return ffmpeg_local
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return "ffmpeg"
    except:
        return None

FFMPEG_PATH = find_ffmpeg()
if FFMPEG_PATH is None:
    print("FFmpeg не найден. Для работы с MP3 и другими форматами установите ffmpeg и добавьте в PATH.")

# --- Класс управления микрофоном ---
class MicrophoneController:
    def __init__(self, input_device_idx, output_devices, volume=100, callback=None):
        """
        output_devices: список словарей с ключами 'index' для устройств вывода
        callback: функция, вызываемая при изменении состояния (для обновления GUI)
        """
        self.input_device_idx = input_device_idx
        self.output_devices = output_devices  # [{'index': idx1}, {'index': idx2}]
        self.volume = volume  # 0-200
        self.callback = callback
        self.active = False
        self.stream = None
        self.output_streams = []
        self.audio_queue = queue.Queue(maxsize=10)
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.active:
            return
        self.stop_event.clear()
        self.active = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        if self.callback:
            self.callback(True)

    def stop(self):
        if not self.active:
            return
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1.0)
        self.active = False
        if self.callback:
            self.callback(False)

    def set_volume(self, vol):
        self.volume = max(0, min(200, vol))

    def set_output_devices(self, devices):
        self.output_devices = devices
        if self.active:
            self.restart()

    def restart(self):
        if self.active:
            self.stop()
            self.start()

    def _run(self):
        try:
            # Параметры: частота 48000, моно, буфер 1024
            samplerate = 48000
            channels = 1
            blocksize = 1024

            # Входной поток
            input_stream = sd.InputStream(
                device=self.input_device_idx,
                samplerate=samplerate,
                channels=channels,
                blocksize=blocksize,
                callback=self._input_callback
            )

            # Выходные потоки для каждого устройства
            output_streams = []
            for dev in self.output_devices:
                if dev and dev.get("index") is not None:
                    try:
                        stream = sd.OutputStream(
                            device=dev["index"],
                            samplerate=samplerate,
                            channels=channels,
                            callback=self._make_output_callback(dev["index"])
                        )
                        output_streams.append(stream)
                    except Exception as e:
                        print(f"Не удалось открыть выходной поток на устройстве {dev['index']}: {e}")

            if not output_streams:
                print("Нет доступных выходных устройств для микрофона")
                self.active = False
                if self.callback:
                    self.callback(False)
                return

            self.output_streams = output_streams

            input_stream.start()
            for s in output_streams:
                s.start()

            # Ожидание сигнала остановки
            while not self.stop_event.is_set():
                time.sleep(0.1)

            input_stream.stop()
            for s in output_streams:
                s.stop()

            input_stream.close()
            for s in output_streams:
                s.close()

        except Exception as e:
            print(f"Ошибка в микрофонном потоке: {e}")
            self.active = False
            if self.callback:
                self.callback(False)

    def _input_callback(self, indata, frames, time, status):
        """Callback входного потока: кладём данные в очередь"""
        if status:
            print(f"Входной статус: {status}")
        # Применяем усиление
        volume_factor = self.volume / 100.0
        amplified = indata * volume_factor
        # Отправляем в очередь для выходных потоков
        try:
            self.audio_queue.put(amplated.copy(), timeout=0.01)
        except queue.Full:
            pass  # пропускаем блок, если переполнение

    def _make_output_callback(self, dev_idx):
        """Создаёт callback для конкретного выходного устройства"""
        def output_callback(outdata, frames, time, status):
            if status:
                print(f"Выходной статус на устройстве {dev_idx}: {status}")
            try:
                data = self.audio_queue.get_nowait()
                # Если данных меньше, чем запрошено, дополняем нулями
                if len(data) < frames:
                    outdata[:len(data)] = data
                    outdata[len(data):] = 0
                else:
                    outdata[:] = data[:frames]
            except queue.Empty:
                outdata.fill(0)
        return output_callback

# --- Воспроизведение на одном устройстве в отдельном потоке ---
def play_on_device(device_idx, samples, samplerate, channels, vu_callback=None):
    pos = 0
    def callback(outdata, frames, time, status):
        nonlocal pos
        remaining = len(samples) - pos
        if remaining <= 0:
            raise sd.CallbackStop
        take = min(frames, remaining)
        chunk = samples[pos:pos+take]
        if chunk.ndim == 1:
            chunk = chunk.reshape(-1, 1)
        outdata[:take] = chunk
        if take < frames:
            outdata[take:] = 0
            raise sd.CallbackStop
        pos += take
        if vu_callback:
            vu_callback(chunk)

    stream = sd.OutputStream(samplerate=samplerate, device=device_idx, channels=channels, callback=callback)
    stream.start()
    while stream.active:
        sd.sleep(10)
    stream.close()

def play_sound(file_path, combo_str=None):
    global vu_level, vu_active, sound_volume
    if not os.path.exists(file_path):
        if app:
            app.root.after(0, lambda: handle_missing_file(file_path))
        return

    if logging_enabled and combo_str is not None:
        log_play(combo_str, file_path)

    try:
        if FFMPEG_PATH and FFMPEG_PATH != "ffmpeg":
            AudioSegment.converter = FFMPEG_PATH

        audio = AudioSegment.from_file(file_path)

        samples = np.array(audio.get_array_of_samples())
        if audio.channels == 2:
            samples = samples.reshape((-1, 2))
        samples_float = samples.astype(np.float32) / (2**(8*audio.sample_width - 1))

        # Применяем громкость соундпада
        samples_float *= (sound_volume / 100.0)

        threads = []

        if primary_device and primary_device["index"] is not None:
            def vu_callback(chunk):
                global vu_level, vu_active
                level = np.sqrt(np.mean(chunk**2))
                vu_level = level * 0.5
                vu_active = True

            t1 = threading.Thread(
                target=play_on_device,
                args=(primary_device["index"], samples_float, audio.frame_rate, audio.channels, vu_callback)
            )
            t1.daemon = True
            t1.start()
            threads.append(t1)

        if secondary_device and secondary_device["index"] is not None:
            if not primary_device or secondary_device["index"] != primary_device["index"]:
                t2 = threading.Thread(
                    target=play_on_device,
                    args=(secondary_device["index"], samples_float, audio.frame_rate, audio.channels, None)
                )
                t2.daemon = True
                t2.start()
                threads.append(t2)

        for t in threads:
            t.join()

        vu_active = False
        vu_level = 0.0

    except Exception as e:
        if "ffmpeg" in str(e).lower() and FFMPEG_PATH is None:
            messagebox.showerror(
                "Ошибка воспроизведения",
                f"Для этого формата требуется ffmpeg.\n"
                f"Скачайте ffmpeg с ffmpeg.org и положите ffmpeg.exe в папку с программой."
            )
        else:
            messagebox.showerror("Ошибка воспроизведения", f"Не удалось воспроизвести {file_path}:\n{e}")

def handle_missing_file(file_path):
    answer = messagebox.askyesno(
        "Файл не найден",
        f"Файл не существует:\n{file_path}\n\nУдалить эту запись из списка?"
    )
    if answer:
        global sounds
        for i, s in enumerate(sounds):
            if s["file"] == file_path:
                del sounds[i]
                break
        app.refresh_list()
        rebuild_hotkey_map()
        mark_changed()

def log_play(combo_str, file_path):
    try:
        with log_lock:
            entry = {
                "timestamp": datetime.now().isoformat(),
                "combination": combo_str,
                "file": file_path
            }
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"Ошибка записи лога: {e}")

def rebuild_hotkey_map():
    with hotkey_lock:
        global hotkey_actions
        hotkey_actions.clear()
        for s in sounds:
            key_str = s.get("key")
            if key_str:
                keys = set(key_str.lower().split('+'))
                hotkey_actions[frozenset(keys)] = {
                    "file": s["file"],
                    "name": s["name"],
                    "combo_str": key_str
                }

def on_global_key_event(e):
    global current_keys, last_triggered_combo
    try:
        if e.event_type == keyboard.KEY_DOWN:
            current_keys.add(e.name)
            with hotkey_lock:
                ignored = ignored_keys.copy()
                actions = hotkey_actions.copy()
            active_keys = current_keys - ignored
            current_frozen = frozenset(active_keys)
            if current_frozen in actions:
                if current_frozen != last_triggered_combo:
                    last_triggered_combo = current_frozen
                    action = actions[current_frozen]
                    threading.Thread(target=play_sound, args=(action["file"], action["combo_str"]), daemon=True).start()
            else:
                last_triggered_combo = None

        elif e.event_type == keyboard.KEY_UP:
            current_keys.discard(e.name)
            last_triggered_combo = None
    except Exception as ex:
        print(f"Ошибка в обработчике клавиш: {ex}")

def setup_global_hotkey_handler():
    global hook_handler
    if hook_handler is None:
        hook_handler = keyboard.hook(on_global_key_event)

def remove_global_hotkey_handler():
    global hook_handler
    if hook_handler is not None:
        keyboard.unhook(hook_handler)
        hook_handler = None

def restart_hotkey_handler():
    """Перезапускает глобальный перехватчик (для кнопки сброса)"""
    remove_global_hotkey_handler()
    setup_global_hotkey_handler()
    print("Обработчик клавиш перезапущен")

# --- Обработка горячей клавиши микрофона ---
def setup_microphone_hotkey():
    global microphone_hotkey_handler
    remove_microphone_hotkey()
    if microphone_hotkey:
        try:
            if microphone_mode == "toggle":
                microphone_hotkey_handler = keyboard.add_hotkey(microphone_hotkey, toggle_microphone)
            else:  # push
                microphone_hotkey_handler = keyboard.add_hotkey(microphone_hotkey, start_microphone, suppress=False, trigger_on_release=False)
                # На отпускание
                keyboard.on_release_key(microphone_hotkey.split('+')[-1], lambda e: stop_microphone() if microphone_active else None)
        except Exception as e:
            print(f"Ошибка регистрации горячей клавиши микрофона: {e}")

def remove_microphone_hotkey():
    global microphone_hotkey_handler
    if microphone_hotkey_handler is not None:
        try:
            keyboard.remove_hotkey(microphone_hotkey_handler)
        except:
            pass
        microphone_hotkey_handler = None

def toggle_microphone():
    if microphone_active:
        stop_microphone()
    else:
        start_microphone()

def start_microphone():
    global microphone_active, microphone_controller
    if microphone_active or not microphone_enabled:
        return
    if microphone_controller is None:
        create_microphone_controller()
    if microphone_controller:
        microphone_controller.start()
        microphone_active = True
        app.update_mic_status()

def stop_microphone():
    global microphone_active, microphone_controller
    if not microphone_active:
        return
    if microphone_controller:
        microphone_controller.stop()
        microphone_active = False
        app.update_mic_status()

def create_microphone_controller():
    global microphone_controller
    if microphone_device_index is None:
        return
    out_devices = []
    if primary_device:
        out_devices.append(primary_device)
    if secondary_device and secondary_device != primary_device:
        out_devices.append(secondary_device)
    if not out_devices:
        return
    microphone_controller = MicrophoneController(
        input_device_idx=microphone_device_index,
        output_devices=out_devices,
        volume=microphone_volume,
        callback=microphone_state_callback
    )

def microphone_state_callback(active):
    global microphone_active
    microphone_active = active
    if app:
        app.root.after(0, app.update_mic_status)

# --- Загрузка/сохранение конфига ---
def load_config():
    global sounds, primary_device, secondary_device, auto_save_enabled, logging_enabled, ignored_keys
    global sound_volume, microphone_volume, microphone_enabled, microphone_device_index, microphone_hotkey, microphone_mode
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            sounds = data.get("sounds", [])
            primary_device = data.get("primary_device")
            secondary_device = data.get("secondary_device")
            auto_save_enabled = data.get("auto_save_enabled", True)
            logging_enabled = data.get("logging_enabled", False)
            ignored_list = data.get("ignored_keys", [])
            with hotkey_lock:
                ignored_keys = set(key.lower().strip() for key in ignored_list if key.strip())
            sound_volume = data.get("sound_volume", 100)
            microphone_volume = data.get("microphone_volume", 100)
            microphone_enabled = data.get("microphone_enabled", False)
            microphone_device_index = data.get("microphone_device_index")
            microphone_hotkey = data.get("microphone_hotkey", "")
            microphone_mode = data.get("microphone_mode", "toggle")
    # Проверка устройств
    devices_info = get_output_devices_info()
    for dev in [primary_device, secondary_device]:
        if dev:
            found = False
            for d in devices_info:
                if d["index"] == dev["index"] and d["name"] == dev["name"] and d["hostapi"] == dev["hostapi"]:
                    found = True
                    break
            if not found:
                messagebox.showwarning(
                    "Предупреждение",
                    f"Сохранённое устройство '{dev['name']}' (индекс {dev['index']}) не найдено.\n"
                    f"Возможно, оно было отключено или переименовано.\n"
                    f"Проверьте настройки устройств."
                )
                if dev is primary_device:
                    primary_device = None
                elif dev is secondary_device:
                    secondary_device = None
                save_config()
                break
    # Проверка устройства ввода
    if microphone_device_index is not None:
        try:
            sd.query_devices(microphone_device_index)
        except:
            messagebox.showwarning(
                "Предупреждение",
                f"Сохранённое устройство ввода (индекс {microphone_device_index}) не найдено.\nМикрофон будет отключён."
            )
            microphone_device_index = None
            microphone_enabled = False
            save_config()

def save_config():
    global need_save, last_change_time
    with hotkey_lock:
        ignored_list = list(ignored_keys)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "sounds": sounds,
            "primary_device": primary_device,
            "secondary_device": secondary_device,
            "auto_save_enabled": auto_save_enabled,
            "logging_enabled": logging_enabled,
            "ignored_keys": ignored_list,
            "sound_volume": sound_volume,
            "microphone_volume": microphone_volume,
            "microphone_enabled": microphone_enabled,
            "microphone_device_index": microphone_device_index,
            "microphone_hotkey": microphone_hotkey,
            "microphone_mode": microphone_mode
        }, f, ensure_ascii=False, indent=4)
    need_save = False
    last_change_time = 0
    update_window_title()

def mark_changed():
    global need_save, last_change_time, auto_save_timer
    need_save = True
    last_change_time = time.time()
    update_window_title()
    if auto_save_enabled:
        if auto_save_timer and auto_save_timer.is_alive():
            pass
        auto_save_timer = threading.Timer(300.0, auto_save)
        auto_save_timer.daemon = True
        auto_save_timer.start()

def auto_save():
    global need_save
    if need_save and auto_save_enabled:
        save_config()

def update_window_title():
    title = "KrimBoard — Soundboard"
    if need_save:
        title += " *"
    app.root.title(title)

def get_output_devices_info():
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

def get_input_devices_info():
    devices = sd.query_devices()
    in_devs = []
    for i, dev in enumerate(devices):
        if dev['max_input_channels'] > 0:
            hostapi = sd.query_hostapis(dev['hostapi'])['name']
            in_devs.append({
                "index": i,
                "name": dev['name'],
                "hostapi": hostapi,
                "display": f"{i}: {dev['name']} ({hostapi})"
            })
    return in_devs

class KrimBoardApp:
    def __init__(self, root):
        self.root = root
        self.root.title("KrimBoard — Soundboard")
        self.root.geometry("950x700")

        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "krimboard_icon.png")
        if os.path.exists(icon_path):
            try:
                icon = tk.PhotoImage(file=icon_path)
                self.root.iconphoto(True, icon)
            except Exception as e:
                print(f"Не удалось загрузить иконку: {e}")

        self.fullscreen = False
        self.capture_handler = None
        self.drag_data = {"item": None, "x": 0, "y": 0}

        load_config()
        rebuild_hotkey_map()
        setup_global_hotkey_handler()
        setup_microphone_hotkey()
        self.root.title("KrimBoard — Soundboard" + (" *" if need_save else ""))

        # Меню
        menubar = tk.Menu(root)
        root.config(menu=menubar)
        view_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Вид", menu=view_menu)
        view_menu.add_command(label="Полноэкранный режим (F11)", command=self.toggle_fullscreen)
        view_menu.add_separator()
        view_menu.add_command(label="Тёмная тема", command=self.toggle_theme)

        # Панель инструментов
        toolbar = ttk.Frame(root)
        toolbar.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)

        ttk.Button(toolbar, text="Добавить файл", command=self.add_sound).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Добавить папку", command=self.add_folder).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Удалить выбранное", command=self.remove_sound).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Назначить клавишу", command=self.assign_key).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Настройки", command=self.settings_dialog).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Сохранить", command=self.manual_save).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Сброс клавиш", command=self.restart_hotkeys).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Сменить тему", command=self.toggle_theme).pack(side=tk.RIGHT, padx=2)

        # Индикатор микрофона
        mic_frame = ttk.Frame(root)
        mic_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=2)
        ttk.Label(mic_frame, text="Микрофон:").pack(side=tk.LEFT, padx=2)
        self.mic_canvas = tk.Canvas(mic_frame, width=20, height=20, bg='gray', highlightthickness=0)
        self.mic_canvas.pack(side=tk.LEFT, padx=5)
        self.mic_indicator = self.mic_canvas.create_oval(2, 2, 18, 18, fill='red', outline='')
        self.update_mic_status()

        # Сортировка
        sort_frame = ttk.Frame(root)
        sort_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=2)
        ttk.Label(sort_frame, text="Сортировка:").pack(side=tk.LEFT, padx=2)
        self.sort_var = tk.StringVar(value="name")
        sort_combo = ttk.Combobox(sort_frame, textvariable=self.sort_var,
                                   values=["По имени", "По дате", "Ручная"],
                                   state="readonly", width=15)
        sort_combo.pack(side=tk.LEFT, padx=2)
        sort_combo.bind("<<ComboboxSelected>>", self.on_sort_change)

        # VU-метр
        vu_frame = ttk.Frame(root)
        vu_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=2)
        ttk.Label(vu_frame, text="Уровень звука:").pack(side=tk.LEFT, padx=2)
        self.vu_canvas = tk.Canvas(vu_frame, width=200, height=20, bg='black', highlightthickness=0)
        self.vu_canvas.pack(side=tk.LEFT, padx=5)
        self.vu_rect = self.vu_canvas.create_rectangle(0, 0, 0, 20, fill='green', width=0)
        self.update_vu_meter()

        # Список звуков
        columns = ("name", "file", "key")
        self.tree = ttk.Treeview(root, columns=columns, show="headings", selectmode="extended")
        self.tree.heading("name", text="Название")
        self.tree.heading("file", text="Файл")
        self.tree.heading("key", text="Горячая клавиша")
        self.tree.column("name", width=250)
        self.tree.column("file", width=500)
        self.tree.column("key", width=150)

        scrollbar = ttk.Scrollbar(root, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10,0), pady=5)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0,10), pady=5)

        # Всплывающая подсказка
        self.tooltip = None
        self.tree.bind("<Enter>", self.on_mouse_enter)
        self.tree.bind("<Leave>", self.on_mouse_leave)
        self.tree.bind("<Motion>", self.on_mouse_motion)

        # Drag and drop
        self.tree.bind("<ButtonPress-1>", self.on_drag_start)
        self.tree.bind("<B1-Motion>", self.on_drag_motion)
        self.tree.bind("<ButtonRelease-1>", self.on_drag_drop)

        self.refresh_list()

        self.root.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        sv_ttk.set_theme("light")
        self.style = ttk.Style()

        if FFMPEG_PATH is None:
            messagebox.showwarning(
                "FFmpeg не найден",
                "Для воспроизведения MP3, OGG и других форматов (кроме WAV) требуется ffmpeg.\n"
                "Скачайте с ffmpeg.org и положите ffmpeg.exe в папку с программой или добавьте в PATH.\n\n"
                "WAV-файлы будут работать без проблем."
            )

    def restart_hotkeys(self):
        """Перезапускает обработчик горячих клавиш"""
        restart_hotkey_handler()
        messagebox.showinfo("Информация", "Обработчик клавиш перезапущен")

    def update_mic_status(self):
        """Обновляет цвет индикатора микрофона"""
        if microphone_active:
            self.mic_canvas.itemconfig(self.mic_indicator, fill='green')
        else:
            self.mic_canvas.itemconfig(self.mic_indicator, fill='red')

    def update_vu_meter(self):
        global vu_level, vu_active
        if vu_active:
            width = int(vu_level * 200)
            if width > 200:
                width = 200
            self.vu_canvas.coords(self.vu_rect, 0, 0, width, 20)
            if width > 150:
                self.vu_canvas.itemconfig(self.vu_rect, fill='red')
            elif width > 80:
                self.vu_canvas.itemconfig(self.vu_rect, fill='yellow')
            else:
                self.vu_canvas.itemconfig(self.vu_rect, fill='green')
        else:
            self.vu_canvas.coords(self.vu_rect, 0, 0, 0, 20)
        self.root.after(50, self.update_vu_meter)

    def on_mouse_enter(self, event): pass
    def on_mouse_leave(self, event):
        if self.tooltip:
            self.tooltip.destroy()
            self.tooltip = None
    def on_mouse_motion(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region == "cell":
            col = self.tree.identify_column(event.x)
            if col == "#2":
                item = self.tree.identify_row(event.y)
                if item:
                    values = self.tree.item(item, "values")
                    if len(values) > 1:
                        file_path = values[1]
                        if len(file_path) > 50:
                            self.show_tooltip(event.widget, file_path, event.x_root + 20, event.y_root + 20)
                            return
        if self.tooltip:
            self.tooltip.destroy()
            self.tooltip = None
    def show_tooltip(self, widget, text, x, y):
        if self.tooltip:
            self.tooltip.destroy()
        self.tooltip = tk.Toplevel(widget)
        self.tooltip.wm_overrideredirect(True)
        self.tooltip.wm_geometry(f"+{x}+{y}")
        label = ttk.Label(self.tooltip, text=text, background="#ffffe0", relief="solid", borderwidth=1)
        label.pack()

    def toggle_theme(self):
        current = sv_ttk.get_theme()
        sv_ttk.set_theme("dark" if current == "light" else "light")

    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        self.root.attributes("-fullscreen", self.fullscreen)

    def manual_save(self):
        save_config()
        messagebox.showinfo("Информация", "Конфигурация сохранена")

    def on_sort_change(self, event):
        mode = self.sort_var.get()
        if mode == "По имени":
            sounds.sort(key=lambda x: x["name"].lower())
        elif mode == "По дате":
            sounds.sort(key=lambda x: os.path.getctime(x["file"]) if os.path.exists(x["file"]) else 0)
        self.refresh_list()
        mark_changed()

    def refresh_list(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        for s in sounds:
            self.tree.insert("", tk.END, values=(s["name"], s["file"], s.get("key", "")))

    def update_sound_in_list(self, index):
        if 0 <= index < len(sounds):
            item_id = self.tree.get_children()[index]
            s = sounds[index]
            self.tree.item(item_id, values=(s["name"], s["file"], s.get("key", "")))

    def on_drag_start(self, event):
        if self.sort_var.get() != "Ручная":
            return
        region = self.tree.identify_region(event.x, event.y)
        if region == "cell":
            self.drag_data["item"] = self.tree.identify_row(event.y)
            self.drag_data["x"] = event.x
            self.drag_data["y"] = event.y

    def on_drag_motion(self, event):
        pass

    def on_drag_drop(self, event):
        if self.sort_var.get() != "Ручная" or not self.drag_data["item"]:
            self.drag_data["item"] = None
            return
        target = self.tree.identify_row(event.y)
        if target and target != self.drag_data["item"]:
            src_index = self.tree.index(self.drag_data["item"])
            dst_index = self.tree.index(target)
            sounds.insert(dst_index, sounds.pop(src_index))
            self.refresh_list()
            mark_changed()
        self.drag_data["item"] = None

    def add_sound(self):
        file_path = filedialog.askopenfilename(filetypes=[("Аудио", "*.wav *.mp3 *.ogg *.flac *.m4a *.aac")])
        if not file_path:
            return
        file_path = os.path.normpath(file_path)
        name = os.path.basename(file_path)
        sounds.append({"name": name, "file": file_path, "key": ""})
        self.refresh_list()
        rebuild_hotkey_map()
        mark_changed()

    def add_folder(self):
        folder_path = filedialog.askdirectory()
        if not folder_path:
            return
        folder_path = os.path.normpath(folder_path)
        audio_exts = ('.wav', '.mp3', '.ogg', '.flac', '.m4a', '.aac')
        added = 0
        for filename in os.listdir(folder_path):
            if filename.lower().endswith(audio_exts):
                full_path = os.path.normpath(os.path.join(folder_path, filename))
                if not any(s["file"] == full_path for s in sounds):
                    sounds.append({"name": filename, "file": full_path, "key": ""})
                    added += 1
        if added > 0:
            self.refresh_list()
            rebuild_hotkey_map()
            mark_changed()
            messagebox.showinfo("Информация", f"Добавлено {added} файлов.")
        else:
            messagebox.showinfo("Информация", "Новых аудиофайлов не найдено.")

    def remove_sound(self):
        selected = self.tree.selection()
        if not selected:
            return
        indices = [self.tree.index(item) for item in selected]
        for idx in sorted(indices, reverse=True):
            del sounds[idx]
        self.refresh_list()
        rebuild_hotkey_map()
        mark_changed()

    def assign_key(self):
        selected = self.tree.selection()
        if not selected or len(selected) > 1:
            messagebox.showwarning("Предупреждение", "Выберите один звук для назначения клавиши")
            return

        self.capture_dialog = tk.Toplevel(self.root)
        self.capture_dialog.title("Назначение клавиши")
        self.capture_dialog.geometry("300x150")
        self.capture_dialog.grab_set()
        sv_ttk.set_theme("dark" if sv_ttk.get_theme() == "dark" else "light")

        ttk.Label(self.capture_dialog, text="Нажмите желаемую комбинацию...").pack(pady=10)
        self.key_label = ttk.Label(self.capture_dialog, text="", font=("Arial", 12))
        self.key_label.pack(pady=5)

        self.capturing = True
        self.pressed_keys = set()
        self.capture_handler = None

        def on_key_event(e):
            if e.event_type == keyboard.KEY_DOWN:
                name = e.name
                if name not in self.pressed_keys:
                    self.pressed_keys.add(name)
            elif e.event_type == keyboard.KEY_UP:
                if self.pressed_keys:
                    combo = "+".join(sorted(self.pressed_keys))
                    self.key_label.config(text=combo)
                    self.capturing = False
                    if self.capture_handler:
                        keyboard.unhook(self.capture_handler)
                    item = selected[0]
                    idx = self.tree.index(item)
                    sounds[idx]["key"] = combo
                    self.update_sound_in_list(idx)
                    rebuild_hotkey_map()
                    mark_changed()
                    self.capture_dialog.destroy()
                self.pressed_keys.clear()

        self.capture_handler = keyboard.hook(on_key_event)
        self.capture_dialog.protocol("WM_DELETE_WINDOW", lambda: self.cancel_capture())
        self.capture_dialog.bind("<Escape>", lambda e: self.cancel_capture())

    def cancel_capture(self):
        self.capturing = False
        if self.capture_handler:
            keyboard.unhook(self.capture_handler)
        self.capture_dialog.destroy()

    def settings_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Настройки")
        dialog.geometry("700x700")
        dialog.grab_set()
        sv_ttk.set_theme("dark" if sv_ttk.get_theme() == "dark" else "light")

        notebook = ttk.Notebook(dialog)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Вкладка "Устройства"
        dev_frame = ttk.Frame(notebook)
        notebook.add(dev_frame, text="Устройства")

        ttk.Label(dev_frame, text="Основное устройство (для Discord):").pack(pady=(10,0))
        devices_info = get_output_devices_info()
        device_displays = [d["display"] for d in devices_info]
        primary_var = tk.StringVar()
        primary_current = 0
        if primary_device:
            for i, d in enumerate(devices_info):
                if d["index"] == primary_device["index"] and d["name"] == primary_device["name"] and d["hostapi"] == primary_device["hostapi"]:
                    primary_current = i
                    break
        primary_combo = ttk.Combobox(dev_frame, textvariable=primary_var, values=device_displays, state="readonly", width=70)
        primary_combo.current(primary_current)
        primary_combo.pack(pady=5)

        ttk.Label(dev_frame, text="Дополнительное устройство (для прослушивания, можно оставить пустым):").pack(pady=(10,0))
        secondary_var = tk.StringVar()
        dev_list_with_empty = [""] + device_displays
        secondary_current = 0
        if secondary_device:
            for i, d in enumerate(devices_info):
                if d["index"] == secondary_device["index"] and d["name"] == secondary_device["name"] and d["hostapi"] == secondary_device["hostapi"]:
                    secondary_current = i + 1
                    break
        secondary_combo = ttk.Combobox(dev_frame, textvariable=secondary_var, values=dev_list_with_empty, state="readonly", width=70)
        secondary_combo.current(secondary_current)
        secondary_combo.pack(pady=5)

        def test_device():
            if primary_var.get():
                selected_display = primary_var.get()
                for d in devices_info:
                    if d["display"] == selected_display:
                        test_device_idx = d["index"]
                        break
                else:
                    messagebox.showerror("Ошибка", "Не удалось определить устройство")
                    return
                duration = 1.0
                samplerate = 44100
                t = np.linspace(0, duration, int(samplerate * duration), endpoint=False)
                tone = 0.3 * np.sin(2 * np.pi * 440 * t)
                if len(tone.shape) == 1:
                    tone = np.column_stack((tone, tone))
                sd.play(tone, samplerate, device=test_device_idx)
                messagebox.showinfo("Тест", "Воспроизводится тестовый сигнал 440 Гц. Вы должны слышать звук.")

        ttk.Button(dev_frame, text="Тест основного устройства", command=test_device).pack(pady=5)

        # Вкладка "Громкость"
        vol_frame = ttk.Frame(notebook)
        notebook.add(vol_frame, text="Громкость")

        ttk.Label(vol_frame, text="Громкость соундпада (0-100%):").pack(pady=(10,0))
        sound_vol_var = tk.IntVar(value=sound_volume)
        sound_vol_scale = ttk.Scale(vol_frame, from_=0, to=100, orient=tk.HORIZONTAL, variable=sound_vol_var, length=300)
        sound_vol_scale.pack(pady=5)
        ttk.Label(vol_frame, textvariable=sound_vol_var).pack()

        ttk.Label(vol_frame, text="Громкость микрофона (0-200%, выше 100% может искажать звук):").pack(pady=(10,0))
        mic_vol_var = tk.IntVar(value=microphone_volume)
        mic_vol_scale = ttk.Scale(vol_frame, from_=0, to=200, orient=tk.HORIZONTAL, variable=mic_vol_var, length=300)
        mic_vol_scale.pack(pady=5)
        ttk.Label(vol_frame, textvariable=mic_vol_var).pack()

        # Вкладка "Микрофон"
        mic_frame = ttk.Frame(notebook)
        notebook.add(mic_frame, text="Микрофон")

        ttk.Label(mic_frame, text="Устройство ввода:").pack(pady=(10,0))
        in_devices = get_input_devices_info()
        in_displays = [d["display"] for d in in_devices]
        mic_device_var = tk.StringVar()
        mic_current = 0
        if microphone_device_index is not None:
            for i, d in enumerate(in_devices):
                if d["index"] == microphone_device_index:
                    mic_current = i
                    break
        mic_combo = ttk.Combobox(mic_frame, textvariable=mic_device_var, values=in_displays, state="readonly", width=70)
        mic_combo.current(mic_current)
        mic_combo.pack(pady=5)

        ttk.Label(mic_frame, text="Горячая клавиша микрофона:").pack(pady=(10,0))
        mic_key_var = tk.StringVar(value=microphone_hotkey)
        mic_key_entry = ttk.Entry(mic_frame, textvariable=mic_key_var, width=30)
        mic_key_entry.pack(pady=5)
        ttk.Button(mic_frame, text="Назначить", command=lambda: self.capture_mic_hotkey(mic_key_var)).pack()

        ttk.Label(mic_frame, text="Режим работы:").pack(pady=(10,0))
        mic_mode_var = tk.StringVar(value=microphone_mode)
        mode_combo = ttk.Combobox(mic_frame, textvariable=mic_mode_var, values=["toggle", "push"], state="readonly", width=15)
        mode_combo.pack(pady=5)

        mic_enable_var = tk.BooleanVar(value=microphone_enabled)
        ttk.Checkbutton(mic_frame, text="Включить микрофон (после применения)", variable=mic_enable_var).pack(pady=10)

        # Вкладка "Прочее"
        misc_frame = ttk.Frame(notebook)
        notebook.add(misc_frame, text="Прочее")

        auto_save_var = tk.BooleanVar(value=auto_save_enabled)
        ttk.Checkbutton(misc_frame, text="Автосохранение каждые 5 минут", variable=auto_save_var).pack(pady=5)

        logging_var = tk.BooleanVar(value=logging_enabled)
        ttk.Checkbutton(misc_frame, text="Вести лог воспроизведений (файл krimboard_log.json)", variable=logging_var).pack(pady=5)

        ttk.Label(misc_frame, text="Игнорируемые клавиши (не влияют на комбинации, разделяйте запятыми):").pack(pady=(10,0))
        with hotkey_lock:
            current_ignored = ", ".join(sorted(ignored_keys))
        ignored_var = tk.StringVar(value=current_ignored)
        ignored_entry = ttk.Entry(misc_frame, textvariable=ignored_var, width=70)
        ignored_entry.pack(pady=5)

        if FFMPEG_PATH is None:
            ttk.Label(misc_frame, text="⚠️ FFmpeg не установлен. Для MP3 и др. форматов требуется ffmpeg.", foreground="red").pack(pady=5)

        def apply_settings():
            global primary_device, secondary_device, auto_save_enabled, logging_enabled, ignored_keys
            global sound_volume, microphone_volume, microphone_enabled, microphone_device_index, microphone_hotkey, microphone_mode

            # Устройства вывода
            primary_sel = primary_var.get()
            if primary_sel:
                for d in devices_info:
                    if d["display"] == primary_sel:
                        primary_device = {"index": d["index"], "name": d["name"], "hostapi": d["hostapi"]}
                        break
            else:
                primary_device = None

            secondary_sel = secondary_var.get()
            if secondary_sel:
                for d in devices_info:
                    if d["display"] == secondary_sel:
                        secondary_device = {"index": d["index"], "name": d["name"], "hostapi": d["hostapi"]}
                        break
            else:
                secondary_device = None

            # Громкость
            sound_volume = sound_vol_var.get()
            microphone_volume = mic_vol_var.get()

            # Микрофон
            mic_sel = mic_device_var.get()
            if mic_sel:
                for d in in_devices:
                    if d["display"] == mic_sel:
                        microphone_device_index = d["index"]
                        break
            else:
                microphone_device_index = None

            microphone_hotkey = mic_key_var.get().strip()
            microphone_mode = mic_mode_var.get()
            microphone_enabled = mic_enable_var.get()

            # Игнорируемые клавиши
            raw_ignored = ignored_var.get()
            ignored_list = [k.strip().lower() for k in raw_ignored.split(',') if k.strip()]
            with hotkey_lock:
                ignored_keys = set(ignored_list)

            # Автосохранение и логирование
            auto_save_enabled = auto_save_var.get()
            logging_enabled = logging_var.get()

            # Перезапуск микрофона, если нужно
            global microphone_controller, microphone_active
            if microphone_active:
                stop_microphone()
            microphone_controller = None
            if microphone_enabled and microphone_device_index is not None:
                create_microphone_controller()
                # Если режим toggle и микрофон должен быть включён постоянно? Нет, оставляем выключенным, пользователь включит горячей клавишей.

            # Обновление горячей клавиши микрофона
            setup_microphone_hotkey()

            save_config()
            dialog.destroy()

        ttk.Button(dialog, text="Применить", command=apply_settings).pack(pady=5)
        ttk.Button(dialog, text="Отмена", command=dialog.destroy).pack()

    def capture_mic_hotkey(self, target_var):
        """Окно захвата горячей клавиши для микрофона"""
        self.capture_dialog = tk.Toplevel(self.root)
        self.capture_dialog.title("Назначение клавиши микрофона")
        self.capture_dialog.geometry("300x150")
        self.capture_dialog.grab_set()
        sv_ttk.set_theme("dark" if sv_ttk.get_theme() == "dark" else "light")

        ttk.Label(self.capture_dialog, text="Нажмите желаемую комбинацию...").pack(pady=10)
        key_label = ttk.Label(self.capture_dialog, text="", font=("Arial", 12))
        key_label.pack(pady=5)

        pressed_keys = set()
        handler = None

        def on_key_event(e):
            nonlocal pressed_keys, handler
            if e.event_type == keyboard.KEY_DOWN:
                name = e.name
                if name not in pressed_keys:
                    pressed_keys.add(name)
            elif e.event_type == keyboard.KEY_UP:
                if pressed_keys:
                    combo = "+".join(sorted(pressed_keys))
                    key_label.config(text=combo)
                    if handler:
                        keyboard.unhook(handler)
                    target_var.set(combo)
                    self.capture_dialog.destroy()
                pressed_keys.clear()

        handler = keyboard.hook(on_key_event)
        self.capture_dialog.protocol("WM_DELETE_WINDOW", lambda: keyboard.unhook(handler) or self.capture_dialog.destroy())
        self.capture_dialog.bind("<Escape>", lambda e: keyboard.unhook(handler) or self.capture_dialog.destroy())

    def on_close(self):
        if need_save and auto_save_enabled:
            save_config()
        remove_global_hotkey_handler()
        remove_microphone_hotkey()
        if microphone_active:
            stop_microphone()
        self.root.destroy()

app = None
if __name__ == "__main__":
    root = tk.Tk()
    app = KrimBoardApp(root)
    root.mainloop()
