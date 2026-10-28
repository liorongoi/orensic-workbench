"""Write docs/linux-server-quick-install.pdf: the short installation guide for a server whose
operating system is already installed (Ubuntu 22.04 / 24.04 or Red Hat 9).

    python deploy/linux/make_quick_guide.py [--out docs/linux-server-quick-install.pdf]

It shares its layout with make_install_guide.py (the full guide, which also covers operating
system preparation, operations and every troubleshooting case).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reportlab.platypus import CondPageBreak  # noqa: E402

from make_install_guide import (A4, ACCENT, PKG, REPO, VERSION, Doc, Image, P, PageBreak,  # noqa: E402
                                ParagraphStyle, Rule, Spacer, box, bullets, checklist, code, h1, mm,
                                small, step, sub, table)

TITLE = f"Lucida {VERSION}: quick installation on a ready Linux server"


def on_page(canvas, doc):
    canvas.saveState()
    if doc.page > 1:
        from make_install_guide import GREY, MINT
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(GREY)
        canvas.drawString(18 * mm, 12 * mm, TITLE)
        canvas.drawRightString(A4[0] - 18 * mm, 12 * mm, f"Page {doc.page}")
        canvas.setStrokeColor(MINT)
        canvas.setLineWidth(0.8)
        canvas.line(18 * mm, 16 * mm, A4[0] - 18 * mm, 16 * mm)
    canvas.restoreState()


def story():
    s = [Spacer(1, 22 * mm)]
    icon = REPO / "brand" / "lucida-icon-256.png"
    if icon.is_file():
        s += [Image(str(icon), width=22 * mm, height=22 * mm, hAlign="LEFT"), Spacer(1, 6 * mm)]
    s += [
        P("Lucida", ParagraphStyle("t0", parent=h1, fontSize=28, leading=34)),
        P("Quick installation on a ready Linux server", ParagraphStyle("t1", parent=h1, fontSize=18, leading=24,
                                                                       textColor=ACCENT)),
        Spacer(1, 3 * mm), Rule(thickness=2), Spacer(1, 5 * mm),
        P(f"Eight steps from a server whose operating system is already installed to examiners using Lucida in "
          f"Chrome. Package: <b>{PKG}.tar.gz</b>. The full guide (<i>linux-server-install.pdf</i>, in the same "
          "folder) has more detail on every step, offline driver installation, everyday operation and "
          "troubleshooting."),
        Spacer(1, 3 * mm),
    ]
    s += table([["This guide assumes", ""],
                ["Operating system", "Ubuntu 22.04 / 24.04 LTS, or Red Hat Enterprise Linux 9 (also Rocky, Alma 9), "
                 "installed, on the network, with a fixed IP address"],
                ["Hardware", "x86_64, 64 GB RAM, NVIDIA L40S"],
                ["Your account", "A user that can use sudo, logged in at the console or with SSH"],
                ["Network", "The server can reach its package repositories (only needed if the NVIDIA driver is not "
                 "installed yet); examiner PCs can reach the server on TCP 8443"],
                ["Examiner PCs", "Google Chrome; administrator rights once, to trust the server's certificate"]],
               [36 * mm, 136 * mm])
    s += [P("Grey boxes are commands: type them on the server unless the box title says otherwise. Green boxes say "
            "what you should see. Where Ubuntu and Red Hat differ, the box title says which; follow only yours.",
            small), PageBreak()]

    # 1 ---------------------------------------------------------------------------------------
    s += step(1, "Confirm the operating system")
    s += [code("grep -E '^(NAME|VERSION_ID)=' /etc/os-release\nuname -m\nhostname -I")]
    s += [box("<i>Ubuntu</i> 22.04 / 24.04, or <i>Red Hat Enterprise Linux</i> (or Rocky / AlmaLinux) 9.x; "
              "<font face='Courier'>x86_64</font>; and the server's IP address. (Version 8 of Red Hat, Rocky or Alma is too old "
              "for this package: upgrade the server to 9 first.) Write the address down: examiners "
              "will type it in Chrome.", "check")]

    # 2 ---------------------------------------------------------------------------------------
    s += step(2, "Make sure the NVIDIA driver works")
    s += [code("nvidia-smi")]
    s += [box("A table naming <i>NVIDIA L40S</i> (about 46068 MiB) with <i>Driver Version</i> 525 or higher "
              "(550+ recommended). If so, skip to step 3. Lucida needs only the driver: do not install the CUDA "
              "toolkit or cuDNN.", "check")]
    s += [P("If <font face='Courier'>nvidia-smi</font> is not found, install the driver, then reboot:")]
    s += [code("""sudo apt update
sudo apt install -y ubuntu-drivers-common pciutils
sudo ubuntu-drivers list --gpgpu          # pick the highest ...-server branch it lists
sudo apt install -y nvidia-driver-570-server nvidia-utils-570-server
sudo reboot""", "Ubuntu")]
    s += [code("""# (Rocky / Alma 9: sudo dnf config-manager --set-enabled crb instead of the next line)
sudo subscription-manager repos --enable codeready-builder-for-rhel-9-x86_64-rpms
sudo dnf install -y \\
  https://dl.fedoraproject.org/pub/epel/epel-release-latest-9.noarch.rpm
sudo dnf install -y kernel-devel-$(uname -r) kernel-headers-$(uname -r) pciutils
sudo dnf config-manager --add-repo \\
  https://developer.download.nvidia.com/compute/cuda/repos/rhel9/x86_64/cuda-rhel9.repo
sudo dnf install -y nvidia-open
sudo reboot""", "Red Hat")]
    s += [box("If Secure Boot is on (<font face='Courier'>mokutil --sb-state</font>), a blue <i>MOK management</i> "
              "screen appears on the console at the reboot: <b>Enroll MOK</b> &gt; <b>Continue</b> &gt; <b>Yes</b> &gt; "
              "the password you chose &gt; <b>Reboot</b>. On Red Hat, first run "
              "<font face='Courier'>sudo mokutil --import /var/lib/dkms/mok.pub</font> to choose that password.", "warn")]
    s += [P("After the reboot, run <font face='Courier'>nvidia-smi</font> again, then turn on persistence mode:")]
    s += [code("sudo systemctl enable --now nvidia-persistenced")]

    # 3 ---------------------------------------------------------------------------------------
    s += step(3, "Copy the package to the server and check it")
    s += [P("Download <b>Lucida-Linux-Server</b> from the repository's <b>Actions</b> tab (workflow <i>Linux server "
            "package</i>, newest green run on <i>main</i>, <i>Artifacts</i>) or from a <b>Release</b>, and unzip it on "
            "your PC. Then copy both files to the server:")]
    s += [code(f"scp {PKG}.tar.gz* <your user>@<server address>:~/", "On your PC (PowerShell or Terminal)")]
    s += [P("On the server (if the release came in parts, first: "
            f"<font face='Courier'>cat {PKG}.tar.gz.part* &gt; {PKG}.tar.gz</font>):")]
    s += [code(f"cd ~\nsha256sum -c {PKG}.tar.gz.sha256\ntar -xzf {PKG}.tar.gz\ncd {PKG}")]
    s += [box(f"<font face='Courier'>{PKG}.tar.gz: OK</font>. If it says <i>FAILED</i>, download it again.", "check")]

    # 4 ---------------------------------------------------------------------------------------
    s += step(4, "Check the server")
    s += [code("./check-server.sh")]
    s += [box("Every line <b>OK</b> and <i>All checks passed</i>. <b>WARN</b> lines are allowed (Lucida works); "
              "fix any <b>FAIL</b> first. The usual ones: an old or missing driver (step 2), or port 8443 in use "
              "(then install with <font face='Courier'>--port 9443</font> in step 5).", "check")]

    # 5 ---------------------------------------------------------------------------------------
    s += step(5, "Install")
    s += [code("sudo ./install.sh")]
    s += [P("About five minutes. It installs Lucida in <font face='Courier'>/opt/lucida</font> (settings in "
            "<font face='Courier'>/etc/lucida/workbench.ini</font>, case data in "
            "<font face='Courier'>/var/lib/lucida</font>), starts the <b>lucida</b> service on HTTPS port 8443 with "
            "sign-in, opens the port in the firewall, runs the self-test and checks the GPU. It ends like this:")]
    s += [code("""RESULT: models run on the GPU

== Done
  Open Lucida in Chrome on an examiner PC at:
      https://10.20.30.40:8443
  First install the certificate authority on each examiner PC (once):
      /var/lib/lucida/tls/workbench-ca.crt  (copy: /home/admin/lucida-ca.crt)
      SHA-256 3A:7F:...:C2
  The first person to open Lucida creates the administrator account.""", "End of the output")]
    s += [P("To keep the case data on a separate disk, mount it first and add <font face='Courier'>--data "
            "/srv/lucida</font>. <font face='Courier'>sudo ./install.sh --help</font> lists every option.", small)]

    # 6 ---------------------------------------------------------------------------------------
    s += step(6, "Trust the server's certificate on each examiner PC")
    s += [P("Once per PC. Copy the certificate the installer left in your home folder on the server, and compare its "
            "SHA-256 with the server's (they must match):")]
    s += [code("scp <your user>@<server address>:lucida-ca.crt .\ncertutil -hashfile lucida-ca.crt SHA256",
               "On a Windows PC (PowerShell)")]
    s += [code("sha256sum ~/lucida-ca.crt", "On the server, to compare")]
    s += [P("Then install it, in a PowerShell opened with <b>Run as administrator</b>, and restart Chrome:")]
    s += [code("certutil -addstore -f Root lucida-ca.crt", "On a Windows PC, as administrator")]
    s += [P("Mac: <font face='Courier'>sudo security add-trusted-cert -d -r trustRoot -k "
            "/Library/Keychains/System.keychain lucida-ca.crt</font>. Linux PC and Group Policy: see the full guide, "
            "step 7.", small)]

    # 7 ---------------------------------------------------------------------------------------
    s += step(7, "Open Lucida in Chrome and create the administrator")
    s += [code("https://<server address>:8443", "In Chrome's address bar")]
    s += [box("Lucida's first-start screen, with no certificate warning. If Chrome says <i>Your connection is not "
              "private</i>, the certificate is not trusted on this PC yet (step 6; restart Chrome completely).", "check")]
    s += bullets(
        "Create the <b>administrator</b> (user name, full name, a password of at least 12 characters). Do it now, "
        "before you share the address.",
        "Your name (top right) &gt; <b>Users...</b>: add each examiner with a role (admin, examiner, reviewer, viewer), "
        "and a <b>second administrator</b> for safe keeping.",
        "Bookmark the address on each PC.")

    # 8 ---------------------------------------------------------------------------------------
    s += step(8, "Confirm and record")
    s += [code("systemctl status lucida      # active (running)\nlucida gpu                   # RESULT: models run on the GPU\nlucida selftest              # All checks passed.")]
    s += [P("The self-test log is saved in <font face='Courier'>/var/lib/lucida/logs</font>; keep it with your lab's "
            "validation records, with the package's SHA-256.")]
    s += [CondPageBreak(70 * mm), sub("If something goes wrong")]
    s += table([["Problem", "Fix"],
                ["Chrome: connection not private", "Step 6 on that PC, then quit and reopen Chrome."],
                ["Chrome: connection timed out", "Firewall. Ubuntu: <font face='Courier'>sudo ufw allow 8443/tcp</font>. "
                 "Red Hat: <font face='Courier'>sudo firewall-cmd --permanent --add-port=8443/tcp; sudo firewall-cmd --reload</font>. "
                 "Also any network firewall between the PCs and the server."],
                ["Chrome: wrong name in certificate, or <i>Invalid host header</i>", "Add the name or address to "
                 "<font face='Courier'>allowed_hosts</font> in /etc/lucida/workbench.ini, then "
                 "<font face='Courier'>sudo systemctl restart lucida</font>."],
                ["Service not running", "<font face='Courier'>journalctl -u lucida -n 80</font> says why."],
                ["<font face='Courier'>lucida gpu</font> says CPU", "<font face='Courier'>nvidia-smi</font> must work "
                 "(step 2); then <font face='Courier'>sudo nvidia-modprobe -u -c=0; sudo systemctl restart lucida</font>."]],
               [52 * mm, 120 * mm])
    s += [sub("Sign-off")]
    s += checklist("<font face='Courier'>nvidia-smi</font> shows the L40S, driver 525 or newer",
                   "Package SHA-256 checked (step 3) and <font face='Courier'>check-server.sh</font> passed",
                   "<font face='Courier'>systemctl status lucida</font>: active (running)",
                   "<font face='Courier'>lucida gpu</font>: models run on the GPU",
                   "Self-test passed; log kept",
                   "Certificate trusted on each examiner PC; Chrome opens Lucida with no warning",
                   "Administrator and a second administrator created; examiners added")
    s += [Spacer(1, 6), P("Installed by: ____________________   Date: ____________   Server: ____________________", small)]
    return s


def build(out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = Doc(str(out), pagesize=A4, leftMargin=19 * mm, rightMargin=19 * mm, topMargin=18 * mm,
              bottomMargin=22 * mm, title=TITLE, author="Lucida",
              subject="Quick installation of Lucida on a Linux server with an NVIDIA L40S")
    doc.multiBuild(story(), onFirstPage=on_page, onLaterPages=on_page)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "docs" / "linux-server-quick-install.pdf"))
    a = ap.parse_args()
    build(Path(a.out))
    print(a.out)
