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

    def run_script(self, lines, scratch_dir, name="sweep-job.sh", timeout=1800):
        """Push a shell script to the phone and run it: no command-line length limits (Windows caps a command at
        32,767 characters; old Android caps adb shell at 4 KB). Returns its output lines."""
        Path(scratch_dir).mkdir(parents=True, exist_ok=True)
        local = Path(scratch_dir) / name
        local.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        remote = f"/data/local/tmp/{name}"
        code, out, err = self.run_(["push", to_host_path(local), remote], 300)
        if code != 0:
            raise PhoneError(f"could not copy a script to the phone: {(err or out).strip()[-200:]}")
        try:
            return self.shell(f"sh {remote}", timeout=timeout).splitlines()
        finally:
            self.run_(["shell", f"rm -f {remote}"], 60)

    def can_hash(self):
        """True when the phone has sha1sum (toybox, Android 6+), which every deletion relies on."""
        out = self.shell("sha1sum /system/build.prop 2>/dev/null || echo NOSHA")
        return len(out.split()[0]) == 40 if out.strip() and not out.startswith("NOSHA") else False

    def delete(self, items, scratch_dir, batch=400, on_batch=None):
        """Delete files on the phone, each only if its SHA-1 on the phone still matches the backed-up copy.
        items: [(phone path, sha1)]. Returns {path: (ok, message)}; on_batch(result_so_far) after each batch."""
        result = {}
        for start in range(0, len(items), batch):
            chunk = items[start:start + batch]
            lines = []
            for i, (p, h) in enumerate(chunk):
                lines.append(f"f={q(p)}; if [ -f \"$f\" ]; then h=$(sha1sum \"$f\" 2>/dev/null); h=${{h%% *}}; h=${{h#\\\\}}; "
                             f"if [ \"$h\" = {q(h)} ]; then rm -f \"$f\" && echo \"OK {i}\" || echo \"FAIL {i}\"; "
                             f"else echo \"CHANGED {i}\"; fi; else echo \"MISSING {i}\"; fi")
            said = {}
            for line in self.run_script(lines, scratch_dir, "sweep-delete.sh"):
                parts = line.split()
                if len(parts) == 2 and parts[1].isdigit() and int(parts[1]) < len(chunk):
                    said[int(parts[1])] = parts[0]
            for i, (p, _) in enumerate(chunk):
                st = said.get(i)
                result[p] = {"OK": (True, "deleted"), "FAIL": (False, "rm failed"),
                             "CHANGED": (False, "the phone's copy differs from the backup, so it was kept"),
                             "MISSING": (False, "not on the phone any more")}.get(st, (False, "no answer from the phone"))
            if on_batch:
                on_batch({p: result[p] for p, _ in chunk})
        return result

    def present(self, paths, scratch_dir):
        """The subset of paths that exist on the phone."""
        if not paths:
            return set()
        lines = [f"[ -f {q(p)} ] && echo \"Y {i}\"" for i, p in enumerate(paths)]
        out = self.run_script(lines, scratch_dir, "sweep-present.sh")
        idx = {int(l.split()[1]) for l in out if l.startswith("Y ") and l.split()[1].isdigit()}
        return {paths[i] for i in idx if i < len(paths)}
