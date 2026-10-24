from sweep.phone import Phone, PhoneError
from sweep import pull
import pytest


class FakeAdb:
    def __init__(self, files, devices="List of devices attached\nABC123\tdevice model:Moto\n"):
        self.files, self.dev, self.calls = files, devices, []

    def __call__(self, args, timeout=0):
        self.calls.append(args)
        if args[:1] == ["devices"]:
            return 0, self.dev, ""
        if args[:1] == ["shell"]:
            c = args[1]
            if c.startswith("[ -d"):
                return 0, ("yes" if any(f.startswith(c.split("'")[1] if "'" in c else c.split()[2]) for f in self.files) else "no"), ""
            if c.startswith("find"):
                root = c.split("'")[1] if "'" in c else c.split()[1]
                return 0, "\n".join(f"{s}|1700000000|{p}" for p, s in self.files.items() if p.startswith(root)), ""
            if c.startswith("getprop"):
                return 0, "Moto G\n", ""
            if c.startswith("df"):
                return 0, "Filesystem 1K-blocks Used Available Use% Mounted\n/dev/x 100 50 2000000 50% /data\n", ""
        if args[:1] == ["pull"]:
            return 0, "pulled", ""
        return 1, "", "unknown"


def test_unauthorized_phone_says_tap_allow():
    ph = Phone(runner=FakeAdb({}, devices="List of devices attached\nABC\tunauthorized\n"))
    with pytest.raises(PhoneError, match="Allow"):
        ph.require_device()


def test_no_phone_says_how_to_connect():
    ph = Phone(runner=FakeAdb({}, devices="List of devices attached\n\n"))
    with pytest.raises(PhoneError, match="USB debugging"):
        ph.require_device()


def test_lists_whatsapp_and_skips_private_and_hidden():
    root = "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images"
    files = {f"{root}/IMG-1.jpg": 100, f"{root}/Sent/IMG-2.jpg": 50, f"{root}/Private/IMG-3.jpg": 70,
             f"{root}/.nomedia": 0}
    ph = Phone(runner=FakeAdb(files))
    rows = ph.list_files(root)
    names = sorted(p.rsplit("/", 1)[-1] for p, _, _ in rows)
    assert names == ["IMG-1.jpg", "IMG-2.jpg"]


def test_sent_folder_is_labelled_separately():
    root = "/r/WhatsApp Images"
    assert pull.folder_label("wa_images", f"{root}/Sent/a.jpg", root) == "wa_sent"
    assert pull.folder_label("wa_images", f"{root}/a.jpg", root) == "wa_images"


def test_free_bytes_parses_df():
    ph = Phone(runner=FakeAdb({}))
    assert ph.free_bytes() == 2000000 * 1024
