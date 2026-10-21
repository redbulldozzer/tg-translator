#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TG Translator v3 — перевод исходящих сообщений (Linux, X11).

Горячая клавиша переводит текст в поле ввода любого приложения:
  * «Только перевести» — текст заменяется переводом, отправляешь сам;
  * «Перевести и отправить» — после замены жмётся Enter.

Языки добавляются одной строкой в словарь LANGS ниже.

Зависимости: python3-tk, xclip, xdotool; pip: pynput requests
"""

import json
import os
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import requests
from pynput import keyboard
from pynput.keyboard import Key, Controller

CONFIG_PATH = os.path.expanduser("~/.config/tg_translator.json")
CLIP_MARK = "\u27ea tgtr-empty \u27eb"

# ------------------------------------------------------------------ языки
# Формат: "Название": {"google": "код google", "deepl": "код deepl" или None}
# None = DeepL этот язык не поддерживает (будет работать только через Google).
# Чтобы добавить язык — просто дописываешь строку.
LANGS = {
    "Английский":    {"google": "en",    "deepl": "EN-US"},
    "Испанский":     {"google": "es",    "deepl": "ES"},
    "Малайский":     {"google": "ms",    "deepl": "MS"},
    "Португальский": {"google": "pt",    "deepl": "PT-BR"},
    "Французский":   {"google": "fr",    "deepl": "FR"},
    "Филиппинский":  {"google": "tl",    "deepl": "TL"},
    "Вьетнамский":   {"google": "vi",    "deepl": "VI"},
    "Арабский":      {"google": "ar",    "deepl": "AR"},
    "Индонезийский": {"google": "id",    "deepl": "ID"},
    "Немецкий":      {"google": "de",    "deepl": "DE"},
    "Итальянский":   {"google": "it",    "deepl": "IT"},
    "Китайский":     {"google": "zh-CN", "deepl": "ZH"},
    "Турецкий":      {"google": "tr",    "deepl": "TR"},
    "Тайский":       {"google": "th",    "deepl": "TH"},
    "Хинди":         {"google": "hi",    "deepl": "HI"},
    "Японский":      {"google": "ja",    "deepl": "JA"},
    "Корейский":     {"google": "ko",    "deepl": "KO"},
    "Польский":      {"google": "pl",    "deepl": "PL"},
    "Русский":       {"google": "ru",    "deepl": "RU"},
}

DEFAULTS = {
    "engine": "google",          # google | deepl
    "deepl_key": "",
    "target_lang": "Английский",
    "hotkey": {"mods": ["ctrl"], "kind": "char", "val": "t", "label": "ctrl+t"},
    "hotkey_mode": "replace",    # replace | send
}

# Раскладка ЙЦУКЕН → латиница: хоткей привязывается к физической клавише,
# поэтому Ctrl+T срабатывает и при включённой русской раскладке (Ctrl+Е).
CYR2LAT = {
    "й": "q", "ц": "w", "у": "e", "к": "r", "е": "t", "н": "y", "г": "u",
    "ш": "i", "щ": "o", "з": "p", "х": "[", "ъ": "]", "ф": "a", "ы": "s",
    "в": "d", "а": "f", "п": "g", "р": "h", "о": "j", "л": "k", "д": "l",
    "ж": ";", "э": "'", "я": "z", "ч": "x", "с": "c", "м": "v", "и": "b",
    "т": "n", "ь": "m", "б": ",", "ю": ".", "ё": "`",
}


def migrate_hotkey(h):
    """Конвертация конфига старого формата ('<ctrl>+t') в новый (dict)."""
    if isinstance(h, dict) and "kind" in h and "val" in h:
        return h
    if isinstance(h, str) and h:
        try:
            tokens = h.split("+")
            mods = [t.strip("<>") for t in tokens[:-1]]
            last = tokens[-1]
            if last.startswith("<"):
                return {"mods": mods, "kind": "key", "val": last.strip("<>"),
                        "label": "+".join(mods + [last.strip("<>")])}
            return {"mods": mods, "kind": "char", "val": last.lower(),
                    "label": "+".join(mods + [last.lower()])}
        except Exception:
            pass
    return dict(DEFAULTS["hotkey"])

kb = Controller()

# Одна сессия на всё время работы: соединение с сервером переиспользуется
# (keep-alive) вместо нового TLS-рукопожатия на каждый перевод.
# Через Tor (Whonix) это экономит 1-2 секунды на каждом запросе после первого.
http = requests.Session()
http.headers.update({"User-Agent": "Mozilla/5.0"})


# ---------------------------------------------------------------- перевод

def translate_google(text: str, lang_name: str) -> str:
    code = LANGS[lang_name]["google"]
    r = http.get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": "auto", "tl": code, "dt": "t", "q": text},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    return "".join(seg[0] for seg in data[0] if seg and seg[0])


def translate_deepl(text: str, lang_name: str, key: str) -> str:
    code = LANGS[lang_name]["deepl"]
    if code is None:
        raise RuntimeError(f"DeepL не поддерживает язык «{lang_name}» — выбери Google")
    key = key.strip()
    host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
    r = http.post(
        f"https://{host}/v2/translate",
        headers={"Authorization": f"DeepL-Auth-Key {key}"},
        data={"text": text, "target_lang": code},
        timeout=15,
    )
    if r.status_code == 403:
        raise RuntimeError("DeepL: неверный API-ключ")
    if r.status_code == 456:
        raise RuntimeError("DeepL: исчерпан месячный лимит")
    r.raise_for_status()
    return r.json()["translations"][0]["text"]


def translate(text: str, cfg: dict) -> str:
    if cfg["engine"] == "deepl":
        return translate_deepl(text, cfg["target_lang"], cfg["deepl_key"])
    return translate_google(text, cfg["target_lang"])


# ---------------------------------------------------------------- системные утилиты

def get_clipboard() -> str:
    try:
        out = subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                             capture_output=True, timeout=2)
        return out.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


def set_clipboard(text: str):
    try:
        subprocess.run(["xclip", "-selection", "clipboard"],
                       input=text.encode("utf-8"), timeout=2)
    except Exception:
        pass


def notify(title: str, msg: str):
    try:
        subprocess.Popen(["notify-send", "-i", "dialog-information", title, msg])
    except Exception:
        pass


def tap(key):
    kb.press(key)
    kb.release(key)


def ctrl_tap(char_or_key):
    with kb.pressed(Key.ctrl):
        tap(char_or_key)


# ---------------------------------------------------------------- движок

class Engine:
    """Один персистентный слушатель клавиатуры. Он же ловит хоткей,
    он же записывает новое сочетание — поэтому что записано, то и сработает.
    Запуск/остановка — просто флаг enabled, никаких join'ов и зависаний."""

    MOD_MAP = {
        Key.ctrl: "ctrl", Key.ctrl_l: "ctrl", Key.ctrl_r: "ctrl",
        Key.alt: "alt", Key.alt_l: "alt", Key.alt_r: "alt", Key.alt_gr: "alt",
        Key.shift: "shift", Key.shift_l: "shift", Key.shift_r: "shift",
        Key.cmd: "cmd", Key.cmd_l: "cmd", Key.cmd_r: "cmd",
    }

    def __init__(self, status_cb):
        self.cfg = dict(DEFAULTS)
        self.status_cb = status_cb
        self.enabled = False
        self.busy = False
        self.pressed_mods = set()
        self.record_cb = None          # если задан — следующее нажатие пишется как хоткей
        self.listener = None
        self.lock = threading.Lock()

    def ensure_listener(self):
        if self.listener is None:
            self.listener = keyboard.Listener(on_press=self.on_press,
                                              on_release=self.on_release)
            self.listener.daemon = True
            self.listener.start()

    def apply(self, cfg: dict):
        hk = cfg.get("hotkey")
        if not (isinstance(hk, dict) and hk.get("kind") and hk.get("val") is not None):
            raise ValueError("сочетание не записано")
        with self.lock:
            self.cfg = dict(cfg)
        self.ensure_listener()
        self.enabled = True

    def disable(self):
        self.enabled = False

    def begin_record(self, callback):
        """Следующее нажатие не-модификатора будет записано как хоткей.
        callback(mods: list, kind: str, val, label: str) — из потока слушателя."""
        self.ensure_listener()
        self.record_cb = callback

    def cancel_record(self):
        self.record_cb = None

    # ---- нормализация клавиши: не зависит от раскладки и Shift'а

    def normalize(self, key):
        if isinstance(key, Key):               # спецклавиша: enter, f9, space...
            return ("key", key.name)
        try:
            k = self.listener.canonical(key)   # снимает эффект Shift
        except Exception:
            k = key
        ch = getattr(k, "char", None)
        if ch:
            ch = ch.lower()
            ch = CYR2LAT.get(ch, ch)           # русская раскладка → физическая клавиша
            return ("char", ch)
        vk = getattr(k, "vk", None) or getattr(key, "vk", None)
        if vk is not None:                     # экзотика без символа — по сканкоду
            return ("vk", int(vk))
        return None

    @staticmethod
    def make_label(mods, kind, val):
        name = f"vk{val}" if kind == "vk" else str(val)
        return "+".join(list(mods) + [name])

    # ---- клавиатура

    def on_press(self, key):
        cat = self.MOD_MAP.get(key)
        if cat:
            self.pressed_mods.add(cat)
            return

        # режим записи сочетания
        if self.record_cb is not None:
            norm = self.normalize(key)
            if norm is None:
                return                          # не смогли опознать — ждём дальше
            cb = self.record_cb
            self.record_cb = None
            mods = sorted(self.pressed_mods)
            kind, val = norm
            cb(mods, kind, val, self.make_label(mods, kind, val))
            return

        if not self.enabled or self.busy:
            return

        hk = self.cfg.get("hotkey") or {}
        norm = self.normalize(key)
        if (norm is not None
                and norm[0] == hk.get("kind")
                and str(norm[1]) == str(hk.get("val"))
                and self.pressed_mods == set(hk.get("mods", []))):
            self.on_hotkey()

    def on_release(self, key):
        cat = self.MOD_MAP.get(key)
        if cat:
            self.pressed_mods.discard(cat)

    def on_hotkey(self):
        if not self.enabled or self.busy:
            return
        send = self.cfg.get("hotkey_mode") == "send"
        threading.Thread(target=self.run_translate, args=(send,),
                         daemon=True).start()

    # ---- перевод текста в активном поле ввода

    def run_translate(self, send: bool):
        if self.busy:
            return
        self.busy = True
        try:
            time.sleep(0.15)                   # дать отпустить клавиши хоткея

            old_clip = get_clipboard()
            set_clipboard(CLIP_MARK)
            time.sleep(0.03)

            ctrl_tap("a")
            time.sleep(0.06)
            ctrl_tap("c")
            time.sleep(0.12)

            text = get_clipboard()
            if text == CLIP_MARK:              # поле пустое
                tap(Key.right)
                set_clipboard(old_clip)
                return

            src = text.strip()
            if not src:
                tap(Key.right)
                set_clipboard(old_clip)
                return

            self.status_cb("Перевожу…")
            try:
                result = translate(src, self.cfg)
            except Exception as e:
                tap(Key.right)                 # снять выделение, оригинал цел
                set_clipboard(old_clip)
                notify("TG Translator: ошибка перевода", str(e))
                self.status_cb(f"Ошибка: {e}")
                return

            set_clipboard(result)
            time.sleep(0.03)
            ctrl_tap("v")
            time.sleep(0.12)

            if send:
                tap(Key.enter)

            time.sleep(0.25)
            set_clipboard(old_clip)
            short = result[:60] + "…" if len(result) > 60 else result
            self.status_cb("Готово: " + short)
        finally:
            self.busy = False


# ---------------------------------------------------------------- GUI

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("TG Translator")
        root.resizable(False, False)

        self.cfg = dict(DEFAULTS)
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    saved = json.load(f)
                self.cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
            except Exception:
                pass
        if self.cfg["target_lang"] not in LANGS:   # конфиг от старой версии
            self.cfg["target_lang"] = DEFAULTS["target_lang"]
        self.cfg["hotkey"] = migrate_hotkey(self.cfg.get("hotkey"))
        self.hotkey_value = dict(self.cfg["hotkey"])

        self.engine = Engine(self.set_status_async)
        self.build_ui()
        self.check_environment()

    # ---- интерфейс

    def build_ui(self):
        pad = {"padx": 8, "pady": 3}
        outer = ttk.Frame(self.root, padding=12)
        outer.grid(sticky="nsew")

        # --- блок: сервис перевода ---
        f_eng = ttk.LabelFrame(outer, text=" Перевод ", padding=8)
        f_eng.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        ttk.Label(f_eng, text="Сервис:").grid(row=0, column=0, sticky="w", **pad)
        self.var_engine = tk.StringVar(value=self.cfg["engine"])
        row = ttk.Frame(f_eng)
        row.grid(row=0, column=1, sticky="w", **pad)
        ttk.Radiobutton(row, text="Google (без ключа)", value="google",
                        variable=self.var_engine,
                        command=self.refresh_state).pack(side="left")
        ttk.Radiobutton(row, text="DeepL", value="deepl",
                        variable=self.var_engine,
                        command=self.refresh_state).pack(side="left", padx=(10, 0))

        ttk.Label(f_eng, text="DeepL API-ключ:").grid(row=1, column=0, sticky="w", **pad)
        self.var_key = tk.StringVar(value=self.cfg["deepl_key"])
        self.ent_key = ttk.Entry(f_eng, textvariable=self.var_key, width=40)
        self.ent_key.grid(row=1, column=1, sticky="w", **pad)

        ttk.Label(f_eng, text="Переводить на:").grid(row=2, column=0, sticky="w", **pad)
        self.var_lang = tk.StringVar(value=self.cfg["target_lang"])
        self.cmb_lang = ttk.Combobox(f_eng, textvariable=self.var_lang,
                                     values=list(LANGS.keys()),
                                     state="readonly", width=18)
        self.cmb_lang.grid(row=2, column=1, sticky="w", **pad)
        self.cmb_lang.bind("<<ComboboxSelected>>", lambda e: self.refresh_state())

        self.lbl_lang_note = ttk.Label(f_eng, foreground="#b30000", text="")
        self.lbl_lang_note.grid(row=3, column=0, columnspan=2, sticky="w", padx=8)

        # --- блок: горячая клавиша ---
        f_hot = ttk.LabelFrame(outer, text=" Горячая клавиша ", padding=8)
        f_hot.grid(row=1, column=0, sticky="ew", pady=(0, 8))

        ttk.Label(f_hot, text="Сочетание:").grid(row=0, column=0, sticky="w", **pad)
        hrow = ttk.Frame(f_hot)
        hrow.grid(row=0, column=1, sticky="w", **pad)
        self.var_hotkey = tk.StringVar(value=self.hotkey_value.get("label", ""))
        self.ent_hotkey = ttk.Entry(hrow, textvariable=self.var_hotkey, width=16,
                                    state="readonly")
        self.ent_hotkey.pack(side="left")
        ttk.Button(hrow, text="Записать",
                   command=self.record_hotkey).pack(side="left", padx=(6, 0))

        ttk.Label(f_hot, text="Действие:").grid(row=1, column=0, sticky="nw", **pad)
        self.var_mode = tk.StringVar(value=self.cfg["hotkey_mode"])
        mrow = ttk.Frame(f_hot)
        mrow.grid(row=1, column=1, sticky="w", **pad)
        ttk.Radiobutton(mrow, text="Только перевести (отправлю сам)",
                        value="replace", variable=self.var_mode).pack(anchor="w")
        ttk.Radiobutton(mrow, text="Перевести и сразу отправить (Enter)",
                        value="send", variable=self.var_mode).pack(anchor="w")

        ttk.Label(f_hot, foreground="#666", justify="left", text=(
            "Пишешь сообщение, жмёшь сочетание — текст в поле\n"
            "заменяется переводом. Работает в любом приложении."
        )).grid(row=2, column=0, columnspan=2, sticky="w", padx=8)

        # --- кнопки ---
        brow = ttk.Frame(outer)
        brow.grid(row=2, column=0, sticky="ew")
        ttk.Button(brow, text="Проверить перевод",
                   command=self.test_translate).pack(side="left")
        self.btn_stop = ttk.Button(brow, text="Остановить", command=self.stop,
                                   state="disabled")
        self.btn_stop.pack(side="right")
        self.btn_start = ttk.Button(brow, text="Сохранить и запустить",
                                    command=self.start)
        self.btn_start.pack(side="right", padx=(0, 6))

        self.status = ttk.Label(outer, text="Остановлен", foreground="#666",
                                wraplength=420, justify="left")
        self.status.grid(row=3, column=0, sticky="w", pady=(8, 0))

        self.refresh_state()

    def refresh_state(self):
        deepl = self.var_engine.get() == "deepl"
        self.ent_key.config(state="normal" if deepl else "disabled")

        lang = self.var_lang.get()
        if deepl and lang in LANGS and LANGS[lang]["deepl"] is None:
            self.lbl_lang_note.config(
                text=f"«{lang}» нет в DeepL — этот язык только через Google")
        else:
            self.lbl_lang_note.config(text="")

    # ---- запись хоткея

    def record_hotkey(self):
        self.var_hotkey.set("нажми сочетание…")

        def on_recorded(mods, kind, val, label):
            # вызывается из потока слушателя → переносим в GUI-поток
            def apply_result():
                if kind == "char" and not mods:
                    self.set_status("Для одиночной клавиши нужен модификатор (Ctrl/Alt)")
                    self.var_hotkey.set(self.hotkey_value.get("label", ""))
                    return
                self.hotkey_value = {"mods": mods, "kind": kind,
                                     "val": val, "label": label}
                self.var_hotkey.set(label)
            self.root.after(0, apply_result)

        self.engine.begin_record(on_recorded)

    # ---- действия

    def collect_cfg(self) -> dict:
        return {
            "engine": self.var_engine.get(),
            "deepl_key": self.var_key.get().strip(),
            "target_lang": self.var_lang.get(),
            "hotkey": dict(self.hotkey_value),
            "hotkey_mode": self.var_mode.get(),
        }

    def validate(self, cfg: dict) -> bool:
        if cfg["engine"] == "deepl" and not cfg["deepl_key"]:
            messagebox.showerror("TG Translator", "Укажи API-ключ DeepL.")
            return False
        if cfg["engine"] == "deepl" and LANGS[cfg["target_lang"]]["deepl"] is None:
            messagebox.showerror(
                "TG Translator",
                f"DeepL не поддерживает язык «{cfg['target_lang']}».\n"
                "Переключись на Google или выбери другой язык.")
            return False
        hk = cfg["hotkey"]
        if not (isinstance(hk, dict) and hk.get("kind") and hk.get("val") is not None):
            messagebox.showerror("TG Translator",
                                 "Запиши горячую клавишу (кнопка «Записать»).")
            return False
        return True

    def start(self):
        cfg = self.collect_cfg()
        if not self.validate(cfg):
            return
        self.cfg.update(cfg)
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=2)

        try:
            self.engine.apply(self.cfg)
        except ValueError as e:
            messagebox.showerror("TG Translator",
                                 f"Некорректное сочетание клавиш: {e}")
            return

        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        act = ("перевести" if self.cfg["hotkey_mode"] == "replace"
               else "перевести и отправить")
        self.set_status(f"Работает: {self.cfg['hotkey'].get('label')} → {act} "
                        f"[{self.cfg['engine']} → {self.cfg['target_lang']}]")

    def stop(self):
        self.engine.disable()
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.set_status("Остановлен")

    def test_translate(self):
        cfg = self.collect_cfg()
        if cfg["engine"] == "deepl" and not cfg["deepl_key"]:
            messagebox.showerror("TG Translator", "Укажи API-ключ DeepL.")
            return
        self.set_status("Проверяю…")

        def worker():
            try:
                res = translate("Привет! Это проверка перевода.", cfg)
                self.set_status_async(f"Тест ОК ({cfg['engine']}): {res}")
            except Exception as e:
                self.set_status_async(f"Тест не прошёл: {e}")

        threading.Thread(target=worker, daemon=True).start()

    # ---- статус и окружение

    def set_status(self, text: str):
        self.status.config(text=text)

    def set_status_async(self, text: str):
        try:
            self.root.after(0, lambda: self.set_status(text))
        except Exception:
            pass

    def check_environment(self):
        problems = []
        if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
            problems.append("Обнаружен Wayland — работает только на X11.")
        for tool in ("xclip", "xdotool"):
            if subprocess.run(["which", tool], capture_output=True).returncode != 0:
                problems.append(f"Не найден {tool} (sudo apt install {tool}).")
        if problems:
            self.set_status("⚠ " + " ".join(problems))


def main():
    root = tk.Tk()
    app = App(root)

    def on_close():
        app.engine.disable()
        root.destroy()
        os._exit(0)

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
