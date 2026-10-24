"""The phone, over adb: list the picture folders, copy them to the laptop, delete what the review approved."""
import os
import shlex
import shutil
import subprocess
from pathlib import Path

WA = "/sdcard/Android/media/com.whatsapp/WhatsApp/Media"
WA_OLD = "/sdcard/WhatsApp/Media"
WA_BIZ = "/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business/Media"

# (label, folder on the phone). The first existing folder of each WhatsApp pair is used.
ROOTS = [
    ("wa_images", f"{WA}/WhatsApp Images"),
    ("wa_images", f"{WA_OLD}/WhatsApp Images"),
    ("wa_images", f"{WA_BIZ}/WhatsApp Business Images"),
    ("wa_video", f"{WA}/WhatsApp Video"),
    ("wa_video", f"{WA_OLD}/WhatsApp Video"),
    ("wa_video", f"{WA_BIZ}/WhatsApp Business Video"),
    ("screenshots", "/sdcard/DCIM/Screenshots"),
    ("screenshots", "/sdcard/Pictures/Screenshots"),
    ("camera", "/sdcard/DCIM/Camera"),
]
SKIP_PARTS = {"Private", ".Statuses", ".trash", ".thumbnails"}


class PhoneError(RuntimeError):
    pass


def find_adb():
    for c in (os.environ.get("SWEEP_ADB"), shutil.which("adb"), shutil.which("adb.exe"),
              "/mnt/c/Users/ASUS/platform-tools-sloth/platform-tools/adb.exe"):
        if c and Path(c).exists():
            return c
    raise PhoneError("adb not found: install Android platform-tools or set SWEEP_ADB to adb's path")


def to_host_path(path):
    """adb.exe on Windows needs a Windows path; Linux adb takes the path as it is."""
    p = str(path)
    if str(find_adb()).endswith(".exe") and p.startswith("/"):
        return subprocess.run(["wslpath", "-w", p], capture_output=True, text=True, check=True).stdout.strip()
    return p


def q(s):
    """Quote for the phone's shell."""
    return shlex.quote(s)


class Phone:
    def __init__(self, serial=None, runner=None):
        self.serial = serial
        self.run_ = runner or self._run

    def _run(self, args, timeout=600):
        cmd = [find_adb()] + (["-s", self.serial] if self.serial else []) + args
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        return r.returncode, r.stdout, r.stderr

    def shell(self, command, timeout=600):
        code, out, err = self.run_(["shell", command], timeout)
        if code != 0 and not out:
            raise PhoneError(err.strip() or f"adb shell failed ({code})")
        return out

    def devices(self):
        code, out, err = self.run_(["devices", "-l"], 30)
        rows = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                rows.append({"serial": parts[0], "state": parts[1], "info": " ".join(parts[2:])})
        return rows

    def require_device(self):
        devs = self.devices()
        if not devs:
            raise PhoneError("No phone found. Turn on USB debugging (Settings > Developer options), plug in the cable, "
                             "and tap Allow on the phone.")
        if self.serial:
            devs = [d for d in devs if d["serial"] == self.serial] or devs
        d = devs[0]
        if d["state"] == "unauthorized":
            raise PhoneError("The phone is asking 'Allow USB debugging?'. Tap Allow (and 'Always allow') on the phone.")
        if d["state"] != "device":
            raise PhoneError(f"Phone state is '{d['state']}'. Unplug and plug it in again.")
        self.serial = d["serial"]
        return d

    def model_name(self):
        return self.shell("getprop ro.product.model").strip() + " / Android " + \
            self.shell("getprop ro.build.version.release").strip()

    def free_bytes(self):
        """Free space on the phone's user storage."""
        out = self.shell("df -k /data")
        lines = [l for l in out.splitlines() if l.strip()]
        cols = lines[-1].split()
        return int(cols[3]) * 1024

    def exists(self, path):
        return self.shell(f"[ -d {q(path)} ] && echo yes || echo no").strip() == "yes"

    def roots(self):
        """(label, phone folder) for each picture folder this phone has; one per WhatsApp label pair."""
        found, seen_wa = [], set()
        for label, path in ROOTS:
            if label.startswith("wa_") and label in seen_wa:
                continue
            if self.exists(path):
                found.append((label, path))
                if label.startswith("wa_"):
                    seen_wa.add(label)
        return found

    def list_files(self, root):
        """[(phone path, bytes, mtime)] under a folder, skipping hidden and private parts."""
        out = self.shell(f"find {q(root)} -type f -exec stat -c '%s|%Y|%n' {{}} +", timeout=900)
        rows = []
        for line in out.splitlines():
            parts = line.split("|", 2)
            if len(parts) != 3 or not parts[0].isdigit():
                continue
            size, mtime, path = int(parts[0]), float(parts[1]), parts[2]
            rel = path[len(root):].lstrip("/").split("/")
            if any(p in SKIP_PARTS or p.startswith(".") for p in rel):
                continue
            rows.append((path, size, mtime))
        return rows

    def pull_dir(self, root, dest_parent):
        """Copy a whole folder; adb puts it at dest_parent/<folder name>."""
        Path(dest_parent).mkdir(parents=True, exist_ok=True)
        code, out, err = self.run_(["pull", "-a", root, to_host_path(dest_parent)], 6 * 3600)
        if code != 0:
            raise PhoneError((err or out).strip()[-500:])
        return out

    def pull_file(self, path, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        code, out, err = self.run_(["pull", "-a", path, to_host_path(dest)], 600)
        if code != 0:
            raise PhoneError((err or out).strip()[-300:])

    def delete(self, paths, batch=200):
        """Delete files on the phone. Returns {path: (ok, message)}. On Android 11+ the gallery updates itself."""
        result = {}
        for i in range(0, len(paths), batch):
            chunk = paths[i:i + batch]
            script = "; ".join(f"rm -f {q(p)} && echo OK:{q(p)} || echo FAIL:{q(p)}" for p in chunk)
            out = self.shell(script, timeout=900)
            said = {}
            for line in out.splitlines():
                if line.startswith("OK:"):
                    said[line[3:]] = (True, "deleted")
                elif line.startswith("FAIL:"):
                    said[line[5:]] = (False, "rm failed")
            for p in chunk:
                result[p] = said.get(p, (False, "no answer from the phone"))
        return result

    def still_there(self, paths):
        script = "; ".join(f"[ -e {q(p)} ] && echo {q(p)}" for p in paths)
        return set(self.shell(script).splitlines()) if paths else set()
