from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import deque
import urllib.parse
import ctypes
import ctypes.wintypes
import json
import math
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
import pyautogui

# Windows (cp1254) terminalinde emoji ve Türkçe karakterlerin çökmesini önle
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import pyperclip
    HAS_PYPERCLIP = True
except ImportError:
    HAS_PYPERCLIP = False

# ============================================================================
# 1. WINDOWS DPI ÖLÇEKLENDİRME DOĞRULAMASI (Per-Monitor DPI Awareness)
# ============================================================================
def dpi_olceklendirme_aktiflestir():
    """Windows 100%/125%/150% DPI ölçeklendirmesinden kaynaklanan koordinat kaymalarını önler."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

dpi_olceklendirme_aktiflestir()

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

def ekran_cozunurlugu_al():
    """Gerçek fiziksel ekran çözünürlüğünü döndürür."""
    w = user32.GetSystemMetrics(0)
    h = user32.GetSystemMetrics(1)
    if w <= 0 or h <= 0:
        w, h = pyautogui.size()
    return int(w), int(h)

screen_w, screen_h = ekran_cozunurlugu_al()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
mouse_lock = threading.Lock()
aktif_durum = True
hedef_gecerli = True
ilk_hareket_loglandi = False
yetki_tam = False

# Tekil Kontrolcü Kilidi (Aynı anda birden fazla sekme veya eski paketlerin imleci saptırmasını önler)
aktif_istemci = {
    "client_id": None,
    "last_seq": -1,
    "last_seen": 0.0
}

imlec_durumu = {
    "x": screen_w // 2,
    "y": screen_h // 2,
    "oran_x": 0.5,
    "oran_y": 0.5,
    "guven": 1.0,
    "durum": "HAZIR",
    "durdurma_nedeni": "",
    "tiklama_anim": 0.0,
    "pencere": {
        "win_x": 0,
        "win_y": 0,
        "outer_w": screen_w,
        "outer_h": screen_h,
        "inner_w": screen_w,
        "inner_h": screen_h,
        "dpr": 1.0
    }
}

# ============================================================================
# 2. HAFİF VE YAPILANDIRILMIŞ HATA AYIKLAMA & LOG SİSTEMİ
# ============================================================================
class SistemLoglayici:
    def __init__(self, max_kayit=150):
        self.kayitlar = deque(maxlen=max_kayit)
        self.son_hareket_log_zamani = 0.0
        self.lock = threading.Lock()

    def kaydet(self, olay: str, detay: dict, konsola_yaz: bool = True):
        zaman = time.strftime("%H:%M:%S")
        veri = {
            "zaman": zaman,
            "olay": olay,
            "algilama_durumu": imlec_durumu["durum"],
            "guven_skoru": round(imlec_durumu["guven"], 3),
            "mevcut_imlec": (imlec_durumu["x"], imlec_durumu["y"]),
            "pencere": imlec_durumu["pencere"],
            **detay
        }
        with self.lock:
            self.kayitlar.append(veri)
        if konsola_yaz:
            ozet = detay.get("mesaj", "")
            print(f"[{zaman}] [{olay}] {ozet}", flush=True)

    def hareket_logla(self, istenen_x: int, istenen_y: int, oran_x: float, oran_y: float):
        simdi = time.time()
        # Konsolu ve belleği şişirmemek için hareket loglarını saniyede en fazla 1 kez kaydet
        if simdi - self.son_hareket_log_zamani >= 1.0:
            self.son_hareket_log_zamani = simdi
            self.kaydet(
                "HAREKET",
                {
                    "istenen_hedef": (istenen_x, istenen_y),
                    "algilanan_oran": (round(oran_x, 4), round(oran_y, 4)),
                    "mesaj": f"Hedef=({istenen_x},{istenen_y}) Oran=({oran_x:.2f},{oran_y:.2f}) Güven={imlec_durumu['guven']:.2f}"
                },
                konsola_yaz=False
            )

    def liste_al(self):
        with self.lock:
            return list(self.kayitlar)

logger = SistemLoglayici()


# ============================================================================
# 3. MERKEZİ KOORDİNAT DÖNÜŞÜM FONKSİYONU
# ============================================================================
def merkezi_koordinat_donustur(oran_x: float, oran_y: float) -> tuple[int, int, bool]:
    """Algılama oranlarını (0.0..1.0) gerçek ekran çözünürlüğüne (screen_w, screen_h) dönüştürür.
    Geçersiz (NaN/Inf) değerleri reddeder ve ekran sınırlarını doğrular."""
    global screen_w, screen_h
    if math.isnan(oran_x) or math.isnan(oran_y) or math.isinf(oran_x) or math.isinf(oran_y):
        return imlec_durumu["x"], imlec_durumu["y"], False

    # Ekran çözünürlüğü değişmişse güncelle
    guncel_w, guncel_h = ekran_cozunurlugu_al()
    if guncel_w > 0 and guncel_h > 0:
        screen_w, screen_h = guncel_w, guncel_h

    clamped_x = max(0.0, min(1.0, float(oran_x)))
    clamped_y = max(0.0, min(1.0, float(oran_y)))

    hedef_x = max(0, min(screen_w - 1, int(round(clamped_x * (screen_w - 1)))))
    hedef_y = max(0, min(screen_h - 1, int(round(clamped_y * (screen_h - 1)))))
    return hedef_x, hedef_y, True


def fare_yetkisi_kontrol() -> bool:
    """Windows üzerinde gerçek fareyi (SetCursorPos) taşıma yetkimiz olup olmadığını test eder."""
    pt = ctypes.wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        return False
    return bool(user32.SetCursorPos(pt.x, pt.y))


def tam_yetkiyle_yeniden_baslat() -> bool:
    """Eğer kısıtlı (Low Integrity) terminalde başlatıldıysa, masaüstündeki klasörü
    baslat.bat seçili olarak ekranın en önünde açar."""
    try:
        bat_path = os.path.join(BASE_DIR, "baslat.bat")
        ret = shell32.ShellExecuteW(None, "open", "explorer.exe", f'/select,"{bat_path}"', BASE_DIR, 1)
        return int(ret) > 32
    except Exception as e:
        logger.kaydet("HATA", {"mesaj": f"Klasör açma hatası: {e}"})
        return False


def gercek_fare_tasi(x: int, y: int) -> int:
    """Hem SetCursorPos hem de donanımsal mouse_event (MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE)
    kullanarak Windows imlecini taşır."""
    res = user32.SetCursorPos(x, y)
    norm_x = int(round(x * 65535 / max(1, screen_w - 1)))
    norm_y = int(round(y * 65535 / max(1, screen_h - 1)))
    user32.mouse_event(0x8001, norm_x, norm_y, 0, 0)
    return res


def gercek_sol_tikla(x: int, y: int):
    """Gerçek Windows konumunda donanımsal sol tık yapar.
    Eğer sağ üst pencere kapatma (X) köşesine (x >= screen_w - 50, y <= 40) tıklandıysa,
    Fitts Yasası gereği (1919, 0) kenar pikseli yerine X düğmesinin tam merkezine (screen_w - 22, 16) tıklar."""
    tik_x, tik_y = x, y
    if x >= screen_w - 50 and y <= 40:
        tik_x = screen_w - 22
        tik_y = 16

    gercek_fare_tasi(tik_x, tik_y)
    norm_x = int(round(tik_x * 65535 / max(1, screen_w - 1)))
    norm_y = int(round(tik_y * 65535 / max(1, screen_h - 1)))
    user32.mouse_event(0x8002, norm_x, norm_y, 0, 0)
    time.sleep(0.025)
    user32.mouse_event(0x8004, norm_x, norm_y, 0, 0)


def eski_sunucuyu_kapat(port: int):
    """Belirtilen portu tutan eski süreç varsa otomatik kapatır."""
    my_pid = os.getpid()
    try:
        out = subprocess.check_output(
            ["netstat", "-ano"], text=True, encoding="utf-8", errors="ignore"
        )
        pids = set()
        for line in out.splitlines():
            if f":{port} " in line and "LISTENING" in line:
                parts = line.split()
                if parts:
                    try:
                        pid = int(parts[-1])
                        if pid != my_pid and pid > 0:
                            pids.add(pid)
                    except ValueError:
                        pass
        for pid in pids:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if pids:
            time.sleep(0.4)
    except Exception:
        pass


def turkce_metin_yaz(metin: str):
    """Türkçe karakterleri destekleyecek şekilde metin yazar."""
    yazi = metin + " "
    if HAS_PYPERCLIP:
        try:
            pyperclip.copy(yazi)
            pyautogui.hotkey('ctrl', 'v')
            return
        except Exception:
            pass
    pyautogui.write(yazi, interval=0.03)


def komutu_uygula(komut: str, params=None):
    """Göz kırpma, pencere kontrolü ve sesli komut aksiyonlarını uygular."""
    global aktif_durum, hedef_gecerli
    if komut in ('hareket', 'durum'):
        return

    if komut == 'hedef_kayboldu':
        neden = params.get('neden', ['Yüz/Göz algılanamadı'])[0] if params else 'Yüz/Göz algılanamadı'
        if hedef_gecerli:
            hedef_gecerli = False
            imlec_durumu["durum"] = "GUVENLI_DURDURULDU"
            imlec_durumu["durdurma_nedeni"] = neden
            logger.kaydet("DURDURULDU", {"durdurma_nedeni": neden, "mesaj": f"Algılama kayboldu, imleç güvenli durduruldu: {neden}"})
        return

    if komut == 'yetki_ac':
        logger.kaydet("YETKI", {"mesaj": "Kullanıcı tam Windows fare yetkisi için klasör açma istedi."})
        threading.Thread(target=tam_yetkiyle_yeniden_baslat, daemon=True).start()
        return

    if komut == 'durdur':
        aktif_durum = False
        imlec_durumu["durum"] = "DURAKLATILDI"
        imlec_durumu["durdurma_nedeni"] = "Kullanıcı tarafından duraklatıldı"
        logger.kaydet("DURUM", {"mesaj": "Göz-Fare kontrolü duraklatıldı."})
        return
    elif komut == 'baslat':
        aktif_durum = True
        imlec_durumu["durum"] = "AKTIF"
        imlec_durumu["durdurma_nedeni"] = ""
        logger.kaydet("DURUM", {"mesaj": "Göz-Fare kontrolü devam ediyor."})
        return

    if not aktif_durum:
        return

    ix = max(0, min(screen_w - 1, int(imlec_durumu["x"])))
    iy = max(0, min(screen_h - 1, int(imlec_durumu["y"])))
    imlec_durumu["tiklama_anim"] = time.time() + 0.35

    logger.kaydet(
        "KOMUT",
        {
            "komut": komut,
            "istenen_hedef": (ix, iy),
            "mesaj": f"İşlem: {komut} -> ({ix}, {iy})"
        }
    )

    if komut in ('tikla', 'sol_tik'):
        gercek_sol_tikla(ix, iy)
    elif komut in ('cift', 'cift_tik'):
        gercek_sol_tikla(ix, iy)
        time.sleep(0.06)
        gercek_sol_tikla(ix, iy)
    elif komut in ('sag', 'sag_tik'):
        gercek_fare_tasi(ix, iy)
        pyautogui.rightClick(ix, iy)
    elif komut in ('asagi', 'asagi_kaydir'):
        pyautogui.scroll(-500)
    elif komut in ('yukari', 'yukari_kaydir'):
        pyautogui.scroll(500)
    elif komut == 'sekme_kapat':
        pyautogui.hotkey('ctrl', 'w')
    elif komut == 'sonraki_sekme':
        pyautogui.hotkey('ctrl', 'tab')
    elif komut == 'onceki_sekme':
        pyautogui.hotkey('ctrl', 'shift', 'tab')
    elif komut == 'pencere_kucult':
        pyautogui.hotkey('win', 'down')
    elif komut == 'pencere_buyut':
        pyautogui.hotkey('f11')
    elif komut in ('kapat', 'pencere_kapat'):
        # Önce sağ üst X düğmesine donanımsal tıklamayı dene, ardından Ctrl+W / Alt+F4 uygula
        gercek_sol_tikla(screen_w - 22, 16)
        pyautogui.hotkey('ctrl', 'w')
    elif komut == 'enter':
        pyautogui.press('enter')
    elif komut == 'sil':
        pyautogui.press('backspace')
    elif komut == 'esc':
        pyautogui.press('esc')
    elif komut == 'yaz' and params and 'metin' in params:
        yazi = params['metin'][0]
        turkce_metin_yaz(yazi)


def ekran_arayuzu_baslat():
    """Tüm Windows ekranı üzerinde gerçek fare imleciyle birlikte gezen ve tıklamaları
    arkaya geçiren şeffaf hedef halkası."""
    try:
        SIZE = 48
        HALF = SIZE // 2
        root = tk.Tk()
        root.title("Göz-Fare İmleç")
        root.overrideredirect(True)
        root.geometry(f"{SIZE}x{SIZE}+{screen_w // 2 - HALF}+{screen_h // 2 - HALF}")
        root.wm_attributes("-topmost", True)
        trans_color = "#010101"
        root.wm_attributes("-transparentcolor", trans_color)
        root.config(bg=trans_color)

        canvas = tk.Canvas(
            root, width=SIZE, height=SIZE, bg=trans_color, highlightthickness=0
        )
        canvas.pack(fill="both", expand=True)

        ring = canvas.create_oval(4, 4, SIZE - 4, SIZE - 4, outline="#00ffcc", width=4)
        dot = canvas.create_oval(HALF - 4, HALF - 4, HALF + 4, HALF + 4, fill="#ff0055", outline="")

        root.update()
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_NOACTIVATE = 0x08000000

        top_hwnd = int(root.wm_frame(), 16) if root.wm_frame() != "0x0" else user32.GetParent(root.winfo_id())
        if top_hwnd:
            style = user32.GetWindowLongW(top_hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                top_hwnd,
                GWL_EXSTYLE,
                style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            )

        def guncelle():
            cx = int(imlec_durumu["x"])
            cy = int(imlec_durumu["y"])
            if not aktif_durum or not hedef_gecerli:
                canvas.itemconfig(ring, outline="#f59e0b", width=3)
                canvas.itemconfig(dot, fill="#f59e0b")
            elif time.time() < imlec_durumu["tiklama_anim"]:
                canvas.itemconfig(ring, outline="#ef4444", width=6)
                canvas.itemconfig(dot, fill="#ef4444")
            else:
                canvas.itemconfig(ring, outline="#00ffcc", width=4)
                canvas.itemconfig(dot, fill="#ff0055")

            root.geometry(f"{SIZE}x{SIZE}+{cx - HALF}+{cy - HALF}")
            root.lift()
            root.wm_attributes("-topmost", True)
            root.after(16, guncelle)

        root.after(16, guncelle)
        root.mainloop()
    except Exception as e:
        logger.kaydet("UYARI", {"mesaj": f"Ekran arayüz uyarısı: {e}"})


class FareKontrol(BaseHTTPRequestHandler):
    def _send_body(self, body: bytes, content_type="application/json; charset=utf-8"):
        try:
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', '*')
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except (ConnectionAbortedError, BrokenPipeError):
            pass

    def do_OPTIONS(self):
        self._send_body(b"", "text/plain; charset=utf-8")

    def do_GET(self):
        global ilk_hareket_loglandi, yetki_tam, hedef_gecerli
        parsed = urllib.parse.urlparse(self.path)
        query = parsed.query
        params = urllib.parse.parse_qs(query)

        if parsed.path in ('/', '/index.html') and not params:
            index_path = os.path.join(BASE_DIR, 'index.html')
            if os.path.exists(index_path):
                with open(index_path, 'rb') as f:
                    content = f.read()
                self._send_body(content, "text/html; charset=utf-8")
                return

        if parsed.path == '/loglar':
            body = json.dumps({
                "ok": True,
                "durum": imlec_durumu,
                "yetki_tam": yetki_tam,
                "screen_w": screen_w,
                "screen_h": screen_h,
                "loglar": logger.liste_al()
            }, ensure_ascii=False).encode('utf-8')
            self._send_body(body, "application/json; charset=utf-8")
            return

        eski_paket_reddedildi = False

        with mouse_lock:
            # 1. Tekil İstemci (Client ID) ve Sıra Numarası (Sequence) Kontrolü
            cid = params.get('cid', [None])[0]
            seq_str = params.get('seq', [None])[0]
            simdi = time.time()

            if cid is not None and seq_str is not None:
                try:
                    seq = int(seq_str)
                    if aktif_istemci["client_id"] != cid:
                        # Yeni sekme / oturum bağlandıysa veya önceki oturum 1.5 saniyedir sessizse devral
                        if aktif_istemci["client_id"] is None or (simdi - aktif_istemci["last_seen"] > 1.5) or seq <= 5:
                            aktif_istemci["client_id"] = cid
                            aktif_istemci["last_seq"] = seq
                            aktif_istemci["last_seen"] = simdi
                        else:
                            eski_paket_reddedildi = True
                    else:
                        if seq <= aktif_istemci["last_seq"] and (aktif_istemci["last_seq"] - seq) < 1000:
                            # Asenkron gecikmeli eski paket! Yeni koordinatı ezmesini engelle.
                            eski_paket_reddedildi = True
                        else:
                            aktif_istemci["last_seq"] = seq
                            aktif_istemci["last_seen"] = simdi
                except ValueError:
                    pass

            # Pencere boyutu / konumu telemetrisi varsa güncelle
            if 'wx' in params and 'wy' in params and 'iw' in params and 'ih' in params:
                try:
                    imlec_durumu["pencere"] = {
                        "win_x": int(float(params['wx'][0])),
                        "win_y": int(float(params['wy'][0])),
                        "outer_w": int(float(params.get('ow', [screen_w])[0])),
                        "outer_h": int(float(params.get('oh', [screen_h])[0])),
                        "inner_w": int(float(params['iw'][0])),
                        "inner_h": int(float(params['ih'][0])),
                        "dpr": float(params.get('dpr', [1.0])[0])
                    }
                except ValueError:
                    pass

            if 'guven' in params:
                try:
                    imlec_durumu["guven"] = max(0.0, min(1.0, float(params['guven'][0])))
                except ValueError:
                    pass

            # 2. Koordinat Dönüşümü ve Hareket Uygulaması
            if not eski_paket_reddedildi and 'x' in params and 'y' in params:
                try:
                    oran_x = float(params['x'][0])
                    oran_y = float(params['y'][0])

                    hedef_x, hedef_y, gecerli = merkezi_koordinat_donustur(oran_x, oran_y)
                    if gecerli and imlec_durumu["guven"] >= 0.35:
                        hedef_gecerli = True
                        imlec_durumu["durum"] = "TAKIP_AKTIF"
                        imlec_durumu["durdurma_nedeni"] = ""
                        imlec_durumu["x"] = hedef_x
                        imlec_durumu["y"] = hedef_y
                        imlec_durumu["oran_x"] = round(oran_x, 4)
                        imlec_durumu["oran_y"] = round(oran_y, 4)

                        logger.hareket_logla(hedef_x, hedef_y, oran_x, oran_y)

                        if aktif_durum:
                            res = gercek_fare_tasi(hedef_x, hedef_y)
                            yetki_tam = bool(res)
                            if not ilk_hareket_loglandi:
                                pt = ctypes.wintypes.POINT()
                                user32.GetCursorPos(ctypes.byref(pt))
                                logger.kaydet(
                                    "BASLANGIC",
                                    {"mesaj": f"İlk Windows imleç hareketi: hedef=({hedef_x},{hedef_y}), gerçek=({pt.x},{pt.y}), yetki={res}"}
                                )
                                ilk_hareket_loglandi = True
                except ValueError:
                    pass

            if not eski_paket_reddedildi and 'islem' in params:
                komut = params['islem'][0]
                komutu_uygula(komut, params)

        response_data = {
            "ok": not eski_paket_reddedildi,
            "aktif": aktif_durum,
            "hedef_gecerli": hedef_gecerli,
            "yetki_tam": yetki_tam,
            "screen_w": screen_w,
            "screen_h": screen_h,
            "x": imlec_durumu["x"],
            "y": imlec_durumu["y"],
            "durum": imlec_durumu["durum"]
        }
        body = json.dumps(response_data).encode('utf-8')
        self._send_body(body, "application/json; charset=utf-8")

    def log_message(self, format, *args):
        return


if __name__ == '__main__':
    PORT = 5050
    eski_sunucuyu_kapat(PORT)
    yetki_tam = fare_yetkisi_kontrol()

    threading.Thread(target=ekran_arayuzu_baslat, daemon=True).start()

    URL = f"http://localhost:{PORT}"
    server = ThreadingHTTPServer(('127.0.0.1', PORT), FareKontrol)

    print("=" * 60, flush=True)
    print("🟢 WINDOWS GÖZ-FARE KÖPRÜSÜ AKTİF!", flush=True)
    print(f"🖥️  Gerçek Ekran Çözünürlüğü (DPI-Aware): {screen_w}x{screen_h}", flush=True)
    print(f"🔐 Windows Fare Kontrol Yetkisi: {'AKTİF (Tam Yetki)' if yetki_tam else 'KISITLI'}", flush=True)
    print(f"🌐 Arayüz Adresi: {URL} | Log Adresi: {URL}/loglar", flush=True)
    print("=" * 60, flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🔴 Göz-Fare Köprüsü kapatıldı.", flush=True)
        server.server_close()
