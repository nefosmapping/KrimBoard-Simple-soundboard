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

CONFIG_FILE = "krimboard_config.json"
sounds = []                # [{"name":..., "file":..., "key":...}, ...]
hotkey_handles = {}
primary_device = None      # словарь: {"index": int, "name": str, "hostapi": str} или None
secondary_device = None    # аналогично
auto_save_enabled = True
need_save = False
last_change_time = 0
auto_save_timer = None

# Глобальная переменная для VU-метра
vu_level = 0.0
vu_active = False

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

# --- Воспроизведение на одном устройстве в отдельном потоке ---
def play_on_device(device_idx, samples, samplerate, channels, vu_callback=None):
    """Воспроизводит samples на указанном устройстве, опционально вызывая vu_callback с фрагментом"""
    pos = 0
    def callback(outdata, frames, time, status):
        nonlocal pos
        remaining = len(samples) - pos
        if remaining <= 0:
            raise sd.CallbackStop
        take = min(frames, remaining)
        chunk = samples[pos:pos+take]
        # Приводим к правильной размерности (frames, channels)
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

def play_sound(file_path):
    global vu_level, vu_active
    if not os.path.exists(file_path):
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
            refresh_hotkeys()
            mark_changed()
        return

    try:
        if FFMPEG_PATH and FFMPEG_PATH != "ffmpeg":
            AudioSegment.converter = FFMPEG_PATH

        audio = AudioSegment.from_file(file_path)

        samples = np.array(audio.get_array_of_samples())
        if audio.channels == 2:
            samples = samples.reshape((-1, 2))
        samples_float = samples.astype(np.float32) / (2**(8*audio.sample_width - 1))

        threads = []

        # Основное устройство (с VU-метром)
        if primary_device and primary_device["index"] is not None:
            def vu_callback(chunk):
                global vu_level, vu_active
                level = np.sqrt(np.mean(chunk**2))
                vu_level = level * 0.5  # масштабируем
                vu_active = True

            t1 = threading.Thread(
                target=play_on_device,
                args=(primary_device["index"], samples_float, audio.frame_rate, audio.channels, vu_callback)
            )
            t1.daemon = True
            t1.start()
            threads.append(t1)

        # Дополнительное устройство (без VU-метра, если отличается от основного)
        if secondary_device and secondary_device["index"] is not None:
            if not primary_device or secondary_device["index"] != primary_device["index"]:
                t2 = threading.Thread(
                    target=play_on_device,
                    args=(secondary_device["index"], samples_float, audio.frame_rate, audio.channels, None)
                )
                t2.daemon = True
                t2.start()
                threads.append(t2)

        # Ждём завершения всех потоков воспроизведения
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

# --- Загрузка/сохранение конфига ---
def load_config():
    global sounds, primary_device, secondary_device, auto_save_enabled
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            sounds = data.get("sounds", [])
            primary_device = data.get("primary_device")
            secondary_device = data.get("secondary_device")
            auto_save_enabled = data.get("auto_save_enabled", True)

    # Проверка сохранённых устройств на валидность
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
                # Сбрасываем проблемное устройство
                if dev is primary_device:
                    primary_device = None
                elif dev is secondary_device:
                    secondary_device = None
                save_config()  # пересохраняем без этого устройства
                break

def save_config():
    global need_save, last_change_time
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "sounds": sounds,
            "primary_device": primary_device,
            "secondary_device": secondary_device,
            "auto_save_enabled": auto_save_enabled
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

# --- Получение списка устройств вывода с детальной информацией ---
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

# --- Горячие клавиши ---
def register_hotkey(key_combo, file_path):
    try:
        handle = keyboard.add_hotkey(key_combo, lambda: play_sound(file_path))
        hotkey_handles[key_combo] = handle
    except Exception as e:
        print(f"Ошибка регистрации {key_combo}: {e}")

def unregister_all_hotkeys():
    for combo, handle in list(hotkey_handles.items()):
        try:
            keyboard.remove_hotkey(handle)
        except Exception as e:
            print(f"Ошибка удаления {combo}: {e}")
    hotkey_handles.clear()

def refresh_hotkeys():
    unregister_all_hotkeys()
    for s in sounds:
        key = s.get("key")
        if key:
            register_hotkey(key, s["file"])

# --- GUI ---
class KrimBoardApp:
    def __init__(self, root):
        self.root = root
        self.root.title("KrimBoard — Soundboard")
        self.root.geometry("950x650")

        self.fullscreen = False
        self.capture_handler = None
        self.drag_data = {"item": None, "x": 0, "y": 0}

        load_config()
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
        ttk.Button(toolbar, text="Сменить тему", command=self.toggle_theme).pack(side=tk.RIGHT, padx=2)

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

        # VU-метр (индикатор уровня)
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
        refresh_hotkeys()

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

    def update_vu_meter(self):
        """Обновляет индикатор уровня громкости"""
        global vu_level, vu_active
        if vu_active:
            width = int(vu_level * 200)  # 200 - ширина canvas
            if width > 200:
                width = 200
            self.vu_canvas.coords(self.vu_rect, 0, 0, width, 20)
            # Меняем цвет в зависимости от уровня
            if width > 150:
                self.vu_canvas.itemconfig(self.vu_rect, fill='red')
            elif width > 80:
                self.vu_canvas.itemconfig(self.vu_rect, fill='yellow')
            else:
                self.vu_canvas.itemconfig(self.vu_rect, fill='green')
        else:
            self.vu_canvas.coords(self.vu_rect, 0, 0, 0, 20)
        self.root.after(50, self.update_vu_meter)

    # Всплывающие подсказки
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

    # Drag and drop
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
        refresh_hotkeys()
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
                    refresh_hotkeys()
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
        dialog.geometry("650x400")
        dialog.grab_set()
        sv_ttk.set_theme("dark" if sv_ttk.get_theme() == "dark" else "light")

        ttk.Label(dialog, text="Основное устройство (для Discord):").pack(pady=(10,0))
        devices_info = get_output_devices_info()
        device_displays = [d["display"] for d in devices_info]
        primary_var = tk.StringVar()
        primary_current = 0
        if primary_device:
            for i, d in enumerate(devices_info):
                if d["index"] == primary_device["index"] and d["name"] == primary_device["name"] and d["hostapi"] == primary_device["hostapi"]:
                    primary_current = i
                    break
        primary_combo = ttk.Combobox(dialog, textvariable=primary_var, values=device_displays, state="readonly", width=70)
        primary_combo.current(primary_current)
        primary_combo.pack(pady=5)

        ttk.Label(dialog, text="Дополнительное устройство (для прослушивания, можно оставить пустым):").pack(pady=(10,0))
        secondary_var = tk.StringVar()
        dev_list_with_empty = [""] + device_displays
        secondary_current = 0
        if secondary_device:
            for i, d in enumerate(devices_info):
                if d["index"] == secondary_device["index"] and d["name"] == secondary_device["name"] and d["hostapi"] == secondary_device["hostapi"]:
                    secondary_current = i + 1
                    break
        secondary_combo = ttk.Combobox(dialog, textvariable=secondary_var, values=dev_list_with_empty, state="readonly", width=70)
        secondary_combo.current(secondary_current)
        secondary_combo.pack(pady=5)

        auto_save_var = tk.BooleanVar(value=auto_save_enabled)
        ttk.Checkbutton(dialog, text="Автосохранение каждые 5 минут", variable=auto_save_var).pack(pady=10)

        if FFMPEG_PATH is None:
            ttk.Label(dialog, text="⚠️ FFmpeg не установлен. Для MP3 и др. форматов требуется ffmpeg.", foreground="red").pack(pady=5)

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

        ttk.Button(dialog, text="Тест основного устройства", command=test_device).pack(pady=5)

        def apply_settings():
            global primary_device, secondary_device, auto_save_enabled
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

            auto_save_enabled = auto_save_var.get()
            save_config()
            dialog.destroy()

        ttk.Button(dialog, text="Применить", command=apply_settings).pack(pady=5)
        ttk.Button(dialog, text="Отмена", command=dialog.destroy).pack()

    def on_close(self):
        if need_save and auto_save_enabled:
            save_config()
        unregister_all_hotkeys()
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = KrimBoardApp(root)
    root.mainloop()