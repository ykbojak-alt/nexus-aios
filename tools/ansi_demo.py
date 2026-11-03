"""ANSI-цвета для консольного вывода демо — чистый stdlib, без зависимостей.

Активны только в интерактивном TTY: NO_COLOR или NEXUS_DEMO_COLOR=0,
редирект в файл/pipe → вывод остаётся чистым текстом (логи и тесты не
ломаются). На Windows включается VT-обработка (os.system("") — стандартный
zero-dep трюк) и utf-8/errors=replace — эмодзи не роняют вывод ни в
консоли, ни в пайпе.

Цвет — только слой отображения: auction_log и чеки остаются plain text,
подкраска идёт при печати (colorize_log), поэтому тесты и JSON-артефакты
не меняются.
"""

from __future__ import annotations

import os
import re
import sys
import time
import time


def _enabled() -> bool:
    if os.environ.get("NO_COLOR") or os.environ.get("NEXUS_DEMO_COLOR") == "0":
        return False
    # FORCE_COLOR / NEXUS_DEMO_COLOR=1 — принудительно, даже в pipe
    if os.environ.get("FORCE_COLOR") or os.environ.get("NEXUS_DEMO_COLOR") == "1":
        return True
    return sys.stdout.isatty()


def _enable_windows_vt() -> None:
    if os.name != "nt":
        return
    try:
        os.system("")
    except Exception:
        pass


# errors=replace: ни один неподдерживаемый символ не роняет print —
# ни в консоли с урезанной кодировкой, ни в пайпе.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

ON = _enabled()
if ON:
    _enable_windows_vt()


def _c(code: str):
    def wrap(text: object) -> str:
        s = str(text)
        return f"\x1b[{code}m{s}\x1b[0m" if ON else s

    return wrap


stage = _c("1;37")      # [n/5] этапы — жирный белый
gold = _c("33")         # [auction] объявления — жёлтый/gold
cyan = _c("36")         # [bid] — голубой
green = _c("1;92")      # победитель / verified=True — ярко-зелёный
magenta = _c("95")      # intent/settle — пурпурный
contract = _c("1;96")   # [contract] — яркий cyan, только буквы
link = _c("1;94")       # TxHash/Explorer — яркий синий, без underline
red = _c("1;91")        # ошибки / verified=False / REJECTED
dim = _c("90")          # разделители, второстепенные логи
# Без SGR 4 (underline) и без фонов 4x/10x: часть терминалов рисует
# подчёркнутый ярко-синий как сплошной синий блок — как зажатую мышь.


def rule(ch: str = "─", n: int = 66) -> str:
    return dim(ch * n)


# ------------------------------------------------------------ typewriter

def slow_print(text: str = "", delay: float = 0.01) -> None:
    """Эффект печатной машинки для видео: посимвольный вывод + sleep.

    Активен только в интерактивном TTY — в pipe/файл и при
    NEXUS_TYPEWRITER=0 вывод обычным print (тесты и логи не тормозят).
    NEXUS_TYPEWRITER=<сек> меняет задержку; =1 форсирует даже в pipe.
    """
    env_d = os.environ.get("NEXUS_TYPEWRITER")
    if env_d == "0":
        print(text, flush=True)
        return
    if env_d and env_d != "1":
        try:
            delay = float(env_d)
        except ValueError:
            pass
    # скорость отдельной ручкой: NEXUS_TYPEWRITER=1 форсирует эффект
    # в pipe, а NEXUS_TYPEWRITER_DELAY задаёт задержку на символ
    env_delay = os.environ.get("NEXUS_TYPEWRITER_DELAY")
    if env_delay:
        try:
            delay = float(env_delay)
        except ValueError:
            pass
    if not sys.stdout.isatty() and env_d != "1":
        # flush=True: в pipe (лог движка → панель) stdout буферизуется
        # блоками ~8 КБ — без flush строки роя всплывали бы пачками,
        # а ответы агентов «застревали» до следующей [llm]-строки.
        print(text, flush=True)
        return
    try:
        for ch in text:
            sys.stdout.write(ch)
            sys.stdout.flush()
            if delay > 0:
                time.sleep(delay)
        sys.stdout.write("\n")
        sys.stdout.flush()
    except UnicodeEncodeError:
        print(str(text).encode("ascii", "replace").decode("ascii"))


# ---------------------------------------------------------------- logo

_LOGO = [
    " ███╗   ██╗███████╗██╗  ██╗██╗   ██╗███████╗",
    " ████╗  ██║██╔════╝╚██╗██╔╝██║   ██║██╔════╝",
    " ██╔██╗ ██║█████╗   ╚███╔╝ ██║   ██║███████╗",
    " ██║╚██╗██║██╔══╝   ██╔██╗ ██║   ██║╚════██║",
    " ██║ ╚████║███████╗██╔╝ ██╗╚██████╔╝███████║",
    " ╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝",
    "      A I   O P E R A T I N G   S Y S T E M",
]

# градиент синий→фиолетовый по строкам лого
_LOGO_CODES = ["1;94", "1;94", "1;95", "1;95", "95", "95", "1;36"]


def logo_lines() -> list[str]:
    """ASCII-арт NEXUS, посиневше-фиолетовый градиент. Без ANSI — plain."""
    out = []
    for i, ln in enumerate(_LOGO):
        code = _LOGO_CODES[min(i, len(_LOGO_CODES) - 1)]
        out.append(f"\x1b[{code}m{ln}\x1b[0m" if ON else ln)
    return out


def colorize_log(line: str) -> str:
    """Семантика строки аукциона → цвет + эмодзи. Слой отображения:
    исходные строки auction_log не трогаем — они нужны тестам и логам
    как чистый текст."""
    pad = "    "
    if "[contract]" in line:
        if "FAILED" in line or "REJECTED" in line:
            return red(f"{pad}⛓️  {line}")
        return contract(f"{pad}⛓️  {line}")
    if "[bid]" in line:
        if "REJECTED" in line:
            return red(f"{pad}🛡️  {line}")
        return cyan(pad + line)
    if "победитель:" in line:
        return green(f"{pad}🏆 {line}")
    if "[auction]" in line:
        if "ни одного" in line or "не выполнена" in line:
            return red(pad + line)
        return gold(pad + line)
    if "[settle]" in line:
        return magenta(f"{pad}⚡ {line}")
    if "[trade]" in line:
        return magenta(f"{pad}📈 {line}")
    if "[guard]" in line:
        return red(f"{pad}🛡️  {line}")
    if "[feed]" in line:
        return dim(pad + line)
    if "[work]" in line:
        return dim(pad + line)
    return pad + line
