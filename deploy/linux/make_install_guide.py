"""Write docs/linux-server-install.pdf, the step-by-step guide for the Linux server package.

    python deploy/linux/make_install_guide.py [--out docs/linux-server-install.pdf]

Needs reportlab (a Lucida dependency). The guide is regenerated from this file, so edit the
text here, not the PDF.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (CondPageBreak, Flowable, Image, KeepTogether, PageBreak, Paragraph,
                                Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle)
from reportlab.platypus.tableofcontents import TableOfContents

REPO = Path(__file__).resolve().parents[2]
VERSION = re.search(r'^version = "(.+)"', (REPO / "backend" / "pyproject.toml").read_text(), re.M).group(1)
PKG = f"Lucida-{VERSION}-linux-x86_64"

INK = colors.HexColor("#0D201C")
ACCENT = colors.HexColor("#1F8A6B")
MINT = colors.HexColor("#8FE3C8")
PALE = colors.HexColor("#EEF8F4")
CODE_BG = colors.HexColor("#F3F4F6")
CODE_EDGE = colors.HexColor("#D1D5DB")
WARN_BG = colors.HexColor("#FFF7E6")
WARN_EDGE = colors.HexColor("#E8A33D")
GREY = colors.HexColor("#5B6470")

ss = getSampleStyleSheet()
body = ParagraphStyle("body", parent=ss["BodyText"], fontName="Helvetica", fontSize=10, leading=14.2,
                      textColor=INK, spaceAfter=5, alignment=TA_LEFT)
small = ParagraphStyle("small", parent=body, fontSize=8.6, leading=11.5, textColor=GREY)
h1 = ParagraphStyle("h1", parent=body, fontName="Helvetica-Bold", fontSize=17, leading=21, spaceBefore=4,
                    spaceAfter=8, textColor=INK)
h2 = ParagraphStyle("h2", parent=body, fontName="Helvetica-Bold", fontSize=12.2, leading=16, spaceBefore=10,
                    spaceAfter=4, textColor=ACCENT)
bullet = ParagraphStyle("bullet", parent=body, leftIndent=14, bulletIndent=3, spaceAfter=3)
code_style = ParagraphStyle("code", fontName="Courier", fontSize=8.7, leading=11.4, textColor=INK)
cell = ParagraphStyle("cell", parent=body, fontSize=8.8, leading=11.6, spaceAfter=0)
cell_b = ParagraphStyle("cellb", parent=cell, fontName="Helvetica-Bold")
toc1 = ParagraphStyle("toc1", parent=body, fontSize=10.5, leading=16, leftIndent=6)


class Heading(Paragraph):
    """A heading that also goes into the table of contents and the PDF outline."""

    def __init__(self, text, style, level):
        super().__init__(text, style)
        self.level = level
        self.plain = re.sub(r"<[^>]+>", "", text)


class Doc(SimpleDocTemplate):
    def afterFlowable(self, f):
        if isinstance(f, Heading):
            key = f"h{id(f)}"
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(f.plain, key, level=f.level)
            if f.level == 0 and f.plain != "Contents":
                self.notify("TOCEntry", (0, f.plain, self.page, key))


class Rule(Flowable):
    def __init__(self, width=None, color=MINT, thickness=1.2):
        super().__init__()
        self.w, self.color, self.t = width, color, thickness

    def wrap(self, aw, ah):
        self.w = self.w or aw
        return self.w, 4

    def draw(self):
        self.canv.setStrokeColor(self.color)
        self.canv.setLineWidth(self.t)
        self.canv.line(0, 2, self.w, 2)


def P(text, style=body):
    return Paragraph(text, style)


def bullets(*items):
    return [Paragraph(i, bullet, bulletText="•") for i in items]


def code(text, title=None):
    """A shell block: what to type. Lines starting with '#' are comments."""
    pre = Preformatted(text.strip("\n"), code_style)
    rows = [[pre]]
    style = [("BACKGROUND", (0, 0), (-1, -1), CODE_BG), ("BOX", (0, 0), (-1, -1), 0.6, CODE_EDGE),
             ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
             ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]
    if title:
        rows.insert(0, [Paragraph(title, ParagraphStyle("ct", parent=small, textColor=colors.white,
                                                        fontName="Helvetica-Bold"))])
        style += [("BACKGROUND", (0, 0), (-1, 0), GREY), ("TOPPADDING", (0, 0), (-1, 0), 3),
                  ("BOTTOMPADDING", (0, 0), (-1, 0), 3)]
    t = Table(rows, colWidths=["100%"])
    t.setStyle(TableStyle(style))
    return KeepTogether([t, Spacer(1, 6)])


def box(text, kind="note"):
    bg, edge, label = {"note": (PALE, ACCENT, "Note"), "warn": (WARN_BG, WARN_EDGE, "Important"),
                       "check": (PALE, ACCENT, "You should see")}[kind]
    t = Table([[Paragraph(f"<b>{label}.</b> {text}", cell)]], colWidths=["100%"])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), bg), ("LINEBEFORE", (0, 0), (0, -1), 3, edge),
                           ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                           ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return KeepTogether([t, Spacer(1, 7)])


def table(rows, widths, header=True, mono=()):
    """mono: the columns shown in Courier (commands, options, paths)."""
    def fmt(i, j, c):
        if header and i == 0:
            return Paragraph(str(c), cell_b)
        return Paragraph(f"<font face='Courier'>{c}</font>" if j in mono else str(c), cell)
    data = [[fmt(i, j, c) for j, c in enumerate(r)] for i, r in enumerate(rows)]
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    style = [("GRID", (0, 0), (-1, -1), 0.4, CODE_EDGE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
             ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5)]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), PALE)]
    t.setStyle(TableStyle(style))
    return [t, Spacer(1, 8)]


def step(n, title):
    return [CondPageBreak(60 * mm), Heading(f"Step {n}. {title}", h1, 0), Rule(), Spacer(1, 6)]


def section(title):
    return [CondPageBreak(60 * mm), Heading(title, h1, 0), Rule(), Spacer(1, 6)]


def sub(title):
    return Heading(title, h2, 1)


def checklist(*items):
    out = []
    for i in items:
        square = Table([[""]], colWidths=[3.2 * mm], rowHeights=[3.2 * mm])
        square.setStyle(TableStyle([("BOX", (0, 0), (0, 0), 0.8, ACCENT)]))
        t = Table([[square, Paragraph(i, ParagraphStyle("ck", parent=body, spaceAfter=0))]],
                  colWidths=[5 * mm, None])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (0, 0), 1.2),
                               ("TOPPADDING", (1, 0), (1, 0), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                               ("LEFTPADDING", (1, 0), (1, 0), 4)]))
        out += [t, Spacer(1, 4)]
    return out


def on_page(canvas, doc):
    canvas.saveState()
    if doc.page > 1:
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(GREY)
        canvas.drawString(18 * mm, 12 * mm, f"Lucida {VERSION}: installing on a Linux server (Ubuntu or Red Hat) with an NVIDIA L40S")
        canvas.drawRightString(A4[0] - 18 * mm, 12 * mm, f"Page {doc.page}")
        canvas.setStrokeColor(MINT)
        canvas.setLineWidth(0.8)
        canvas.line(18 * mm, 16 * mm, A4[0] - 18 * mm, 16 * mm)
    canvas.restoreState()


def cover():
    out = [Spacer(1, 30 * mm)]
    icon = REPO / "brand" / "lucida-icon-256.png"
    if icon.is_file():
        out += [Image(str(icon), width=26 * mm, height=26 * mm, hAlign="LEFT"), Spacer(1, 8 * mm)]
    out += [
        P("Lucida", ParagraphStyle("t0", parent=h1, fontSize=30, leading=36)),
        P("Installing the web app on a Linux server with an NVIDIA L40S (Ubuntu or Red Hat)",
          ParagraphStyle("t1", parent=h1, fontSize=18, leading=24, textColor=ACCENT)),
        Spacer(1, 4 * mm), Rule(thickness=2), Spacer(1, 6 * mm),
        P(f"Step-by-step guide for package <b>{PKG}.tar.gz</b>. It takes you from a server with the "
          "operating system already installed to examiners using Lucida in Chrome on their own PCs: "
          "the NVIDIA driver, getting and checking the package, installing the service, trusting its "
          "certificate, the first sign-in, the self-test, everyday operation and troubleshooting."),
        Spacer(1, 4 * mm),
        *table([["Server", "x86_64, 64 GB RAM, NVIDIA L40S (48 GB), with Ubuntu Server 22.04 / 24.04 LTS or Red Hat Enterprise Linux 8 / 9 (Rocky Linux and AlmaLinux work the same way as Red Hat)"],
                ["One package", "The same package installs on both families: it carries its own Python, CUDA libraries and FFmpeg. Only the NVIDIA driver installation differs, and this guide gives both"],
                ["Examiner PCs", "Google Chrome (or Edge) on Windows, macOS or Linux, on the same network"],
                ["Time needed", "About 30 minutes, plus a reboot if the NVIDIA driver is not installed yet"],
                ["Internet", "Only to install the NVIDIA driver (step 2). "
                             "Lucida itself installs and runs fully offline"]],
               [32 * mm, 140 * mm], header=False),
        Spacer(1, 20 * mm),
        P("How to read this guide: grey boxes are commands to type on the server (or on a PC, where the "
          "box title says so). Type them exactly; lines starting with <font face='Courier'>#</font> are "
          "comments and need not be typed. Green boxes say what you should see. Orange boxes are things "
          "that matter for security or for evidence. Where Ubuntu and Red Hat differ, the box title says "
          "<b>Ubuntu</b> or <b>Red Hat</b>: follow only the one for your server.", small),
        PageBreak(),
    ]
    return out


def content():
    s = []
    s += [Heading("Contents", h1, 0), Rule(), Spacer(1, 4)]
    toc = TableOfContents()
    toc.levelStyles = [toc1]
    s += [toc, PageBreak()]

    # ------------------------------------------------------------------------------------------
    s += section("Overview")
    s += [P("Lucida runs on the server as a system service called <b>lucida</b>. It serves its web interface "
            "over HTTPS on port <b>8443</b>. Examiners open <font face='Courier'>https://&lt;server address&gt;:8443</font> "
            "in Chrome on their own PCs; nothing is installed on those PCs except, once, the server's "
            "certificate authority, so Chrome trusts the connection. Evidence never leaves the server.")]
    s += [sub("What the package contains")]
    s += bullets(
        "A private Python 3.12 with every library Lucida uses. It does not touch the system's Python.",
        "ONNX Runtime for CUDA 12 with NVIDIA's CUDA 12 and cuDNN 9 libraries. The server therefore needs "
        "<b>only the NVIDIA driver</b>: no CUDA toolkit and no cuDNN installation.",
        "Static FFmpeg and FFprobe for video, the built web interface, the AI models (each pinned by "
        "SHA-256 and re-checked before every use), the documentation and the licences.",
        "<font face='Courier'>install.sh</font>, <font face='Courier'>check-server.sh</font> and, once "
        "installed, <font face='Courier'>/opt/lucida/uninstall.sh</font> and the "
        "<font face='Courier'>lucida</font> command.")
    s += [sub("Where things go")]
    s += table([["What", "Where", "Kept on upgrade / uninstall"],
                ["Programs", "/opt/lucida", "Replaced / removed"],
                ["Settings", "/etc/lucida/workbench.ini", "Kept / kept (removed with --purge)"],
                ["Case data: evidence, derived files, audit log, accounts", "/var/lib/lucida", "Kept / kept (removed with --purge)"],
                ["HTTPS certificate authority", "/var/lib/lucida/tls/workbench-ca.crt", "Kept / kept"],
                ["Self-test logs", "/var/lib/lucida/logs", "Kept / kept"],
                ["Service definition", "/etc/systemd/system/lucida.service", "Replaced / removed"],
                ["Service log", "journalctl -u lucida", "System journal"]],
               [62 * mm, 62 * mm, 48 * mm], mono=(1,))
    s += [sub("The GPU and reproducibility")]
    s += [P("The learned models (super-resolution, face and object detection, plate reading) run on the L40S "
            "through CUDA. Lucida runs each model twice when it first loads it and uses the GPU only if both "
            "runs give the same bits; otherwise that model runs on the CPU. On the GPU it uses deterministic "
            "kernels and full 32-bit precision (no TF32), so a re-run after a restart gives the same result. "
            "Classical filters always run on the CPU, on every core, because they give the same bits on every "
            "machine. The report records where every step ran.")]
    s += [sub("Before you start: checklist")]
    s += checklist(
        "You can log in to the server as a user that may use <font face='Courier'>sudo</font> (on Red Hat: a "
        "member of <i>wheel</i>) "
        "(at the console, or with SSH from your PC).",
        "You know the server's IP address on the lab network, or will find it in step 1.",
        "The server can reach its package repositories (Ubuntu's archive, or Red Hat's plus NVIDIA's) for the "
        "NVIDIA driver (step 2), or the driver is "
        "already installed. If the server is fully offline, see <i>Offline driver installation</i> in step 2.",
        "You can download files from GitHub on some PC (step 3).",
        "Examiner PCs can reach the server on TCP port 8443 (no firewall between them blocks it).",
        "At least 20 GB free for the case data to start; 500 GB or more for real casework. A separate data "
        "disk is a good idea (see step 5).")

    # ------------------------------------------------------------------------------------------
    s += step(1, "Log in to the server and find out which Linux it runs")
    s += [P("Log in at the server's console, or from your PC with SSH. Windows 10 and 11 include "
            "<font face='Courier'>ssh</font>: open <b>PowerShell</b> or <b>Command Prompt</b> and type:")]
    s += [code("ssh <your user>@<server address>\n# for example:  ssh admin@10.20.30.40", "On your PC")]
    s += [sub("1.1 Which operating system?")]
    s += [code("cat /etc/os-release")]
    s += table([["If you see", "The server runs", "Follow the boxes marked"],
                ["NAME=\"Ubuntu\", VERSION_ID=\"22.04\" or \"24.04\"", "Ubuntu Server LTS", "<b>Ubuntu</b>"],
                ["NAME=\"Red Hat Enterprise Linux\", VERSION_ID=\"9.x\" or \"8.x\"", "Red Hat Enterprise Linux 9 or 8", "<b>Red Hat</b>"],
                ["NAME=\"Rocky Linux\" or \"AlmaLinux\", VERSION_ID 9.x or 8.x", "A Red Hat rebuild", "<b>Red Hat</b> (differences noted)"]],
               [72 * mm, 50 * mm, 50 * mm])
    s += [P("Write down the family and the major version (for example <i>Red Hat 9</i>); step 2 depends on it. "
            "Everything from step 3 on is the same on both, except where a box says otherwise.")]
    s += [sub("1.2 Processor, memory, disk and network")]
    s += [code("""uname -m                 # processor architecture
free -g                  # memory in GB
df -h / /var /opt        # free disk space
hostname -I              # the server's IP addresses
hostname -f              # the server's full name""")]
    s += [box("<font face='Courier'>x86_64</font>; about 62 in the <i>total</i> column of <i>Mem</i> "
              "(64 GB less what the firmware keeps); one or more addresses such as "
              "<font face='Courier'>10.20.30.40</font>. Write down the address examiners will use.", "check")]
    s += [sub("1.3 Bring the system up to date")]
    s += [P("If the server can reach the internet (or your lab's package mirror), update it first. This is normal "
            "maintenance, and the driver in step 2 must be built for the kernel that is running, so reboot after a "
            "kernel update before you continue.")]
    s += [code("sudo apt update\nsudo apt -y full-upgrade\nsudo reboot", "Ubuntu")]
    s += [code("""sudo subscription-manager status     # Red Hat only: the server must be registered
sudo dnf -y upgrade
sudo reboot""", "Red Hat (and Rocky / Alma)")]
    s += [box("Make sure the clock is right: <font face='Courier'>timedatectl</font> should say "
              "<i>System clock synchronized: yes</i>. Audit-log entries and reports take their time from "
              "this clock. If it says no, point the server at your lab's time server: on Ubuntu edit "
              "<font face='Courier'>/etc/systemd/timesyncd.conf</font> (<font face='Courier'>NTP=&lt;time server&gt;</font>) and "
              "<font face='Courier'>sudo systemctl restart systemd-timesyncd</font>; on Red Hat edit "
              "<font face='Courier'>/etc/chrony.conf</font> (<font face='Courier'>server &lt;time server&gt; iburst</font>) and "
              "<font face='Courier'>sudo systemctl restart chronyd</font>.", "warn")]

    # ------------------------------------------------------------------------------------------
    s += step(2, "Install and check the NVIDIA driver")
    s += [P("Lucida needs the NVIDIA driver, version <b>525 or newer</b> (550 or newer recommended for the "
            "L40S). It does <b>not</b> need the CUDA toolkit or cuDNN: those libraries are inside the package.")]
    s += [sub("2.1 Is the card visible, and is a driver already installed?")]
    s += [code("lspci | grep -i nvidia\nnvidia-smi")]
    s += [box("<font face='Courier'>lspci</font> lists a line with <i>NVIDIA Corporation AD102GL [L40S]</i>. "
              "If <font face='Courier'>nvidia-smi</font> prints a table naming <i>NVIDIA L40S</i> with about "
              "<i>46068MiB</i> of memory and a <i>Driver Version</i> of 550 or higher, the driver is ready: go "
              "to step 2.4.", "check")]
    s += [P("If <font face='Courier'>lspci</font> shows nothing, the card is not seen by the server at all: check "
            "it is seated and powered, and that it is enabled in the BIOS (for a virtual machine: that the GPU is "
            "passed through to it). If <font face='Courier'>nvidia-smi</font> says <i>command not found</i>, "
            "install the driver (2.2). If it says <i>couldn't communicate with the NVIDIA driver</i>, see 2.3.")]
    s += [sub("2.2 Ubuntu: install the driver from Ubuntu's archive")]
    s += [P("Ubuntu ships NVIDIA's data-centre drivers (the <i>-server</i> branches, which are the right ones "
            "for an L40S). Let Ubuntu list what fits the card, then install the newest <i>-server</i> branch "
            "it offers:")]
    s += [code("""sudo apt update
sudo apt install -y ubuntu-drivers-common pciutils
sudo ubuntu-drivers list --gpgpu
# the list shows names such as nvidia-driver-570-server and nvidia-driver-580-server.
# Install the highest number that ends in -server, for example:
sudo apt install -y nvidia-driver-570-server nvidia-utils-570-server
sudo reboot""", "Ubuntu")]
    s += [P("Alternatively <font face='Courier'>sudo ubuntu-drivers install --gpgpu</font> picks the recommended "
            "branch on its own. Use one method, not both.")]
    s += [sub("2.2 Red Hat: install the driver from NVIDIA's repository")]
    s += [P("Red Hat does not ship the NVIDIA driver; NVIDIA publishes it as a dnf repository. The driver is "
            "compiled for your kernel by DKMS, which comes from EPEL. For the L40S, NVIDIA recommends its "
            "<i>open</i> kernel modules. The commands are for version 9; <b>on version 8 replace every 9 with 8</b>.")]
    s += [code("""# 1. the CodeReady Builder repository (needed by EPEL)
sudo subscription-manager repos --enable codeready-builder-for-rhel-9-x86_64-rpms
#    Rocky / Alma 9 instead:  sudo dnf config-manager --set-enabled crb
#    Rocky / Alma 8 instead:  sudo dnf config-manager --set-enabled powertools
# 2. EPEL, for DKMS
sudo dnf install -y \\
  https://dl.fedoraproject.org/pub/epel/epel-release-latest-9.noarch.rpm
# 3. the build tools for the running kernel, and lspci
sudo dnf install -y kernel-devel-$(uname -r) kernel-headers-$(uname -r) pciutils
# 4. NVIDIA's repository
sudo dnf config-manager --add-repo \\
  https://developer.download.nvidia.com/compute/cuda/repos/rhel9/x86_64/cuda-rhel9.repo
sudo dnf clean expire-cache
# 5. the driver (open kernel modules)
sudo dnf install -y nvidia-open                            # version 9
#   version 8 instead:  sudo dnf module install -y nvidia-driver:open-dkms
sudo reboot""", "Red Hat 9 (and Rocky / Alma 9); for 8, replace 9 with 8")]
    s += [P("If step 3 says no package matches <font face='Courier'>kernel-devel-...</font>, the running kernel is "
            "older than the newest available: run <font face='Courier'>sudo dnf -y upgrade</font>, reboot, and repeat "
            "from step 3. NVIDIA's proprietary modules (<font face='Courier'>sudo dnf install -y cuda-drivers</font>) "
            "work for Lucida too; choose one kind, not both.")]
    s += [box("<b>Secure Boot.</b> Check with <font face='Courier'>mokutil --sb-state</font>. If it says "
              "<i>SecureBoot enabled</i>, the kernel only loads signed modules. "
              "<b>Ubuntu:</b> the driver installation asks you to choose a password. "
              "<b>Red Hat:</b> DKMS signs the module with its own key; register that key with "
              "<font face='Courier'>sudo mokutil --import /var/lib/dkms/mok.pub</font> and choose a password. "
              "On both, at the next reboot a blue <i>MOK management</i> screen appears on the server's console (use "
              "the BMC / iDRAC / iLO remote console): choose <b>Enroll MOK</b>, <b>Continue</b>, <b>Yes</b>, type that "
              "password, then <b>Reboot</b>. If you miss that screen the driver will not load; on Ubuntu run "
              "<font face='Courier'>sudo update-secureboot-policy --enroll-key</font>, on Red Hat the "
              "<font face='Courier'>mokutil --import</font> command again, and reboot.", "warn")]
    s += [box("Do not install the CUDA toolkit (<font face='Courier'>nvidia-cuda-toolkit</font>, "
              "<font face='Courier'>cuda</font>, <font face='Courier'>cuda-toolkit-*</font>) or cuDNN for Lucida. "
              "They are not needed, and a second copy of the CUDA libraries can confuse other software on the "
              "server. Only the driver is needed.", "note")]
    s += [sub("2.3 After the reboot: check the driver")]
    s += [code("nvidia-smi\nnvidia-smi --query-gpu=name,driver_version,memory.total,persistence_mode --format=csv")]
    s += [box("<font face='Courier'>NVIDIA L40S, 570.xx.xx, 46068 MiB, ...</font> (the numbers depend on the "
              "driver branch). The table's top line also shows <i>CUDA Version: 12.x</i> or higher; that is the "
              "highest CUDA the driver supports, not something you install.", "check")]
    s += [P("If <font face='Courier'>nvidia-smi</font> still cannot talk to the driver:")]
    s += bullets(
        "<font face='Courier'>lsmod | grep nouveau</font> shows a line: the open-source driver is still "
        "loaded. The NVIDIA package disables it; rebuild the boot image and reboot: Ubuntu "
        "<font face='Courier'>sudo update-initramfs -u</font>, Red Hat <font face='Courier'>sudo dracut -f</font>.",
        "<font face='Courier'>sudo dmesg | grep -i -E 'nvidia|secure'</font> mentions <i>Lockdown</i>, <i>key</i> "
        "or <i>signature</i>: Secure Boot rejected the module; enrol the key as described above.",
        "<font face='Courier'>dkms status</font> shows the module was not built for the running kernel: "
        "Ubuntu <font face='Courier'>sudo apt install --reinstall nvidia-dkms-570-server</font> (your branch "
        "number); Red Hat: install <font face='Courier'>kernel-devel-$(uname -r)</font>, then "
        "<font face='Courier'>sudo dkms autoinstall</font>; then reboot.")
    s += [sub("2.4 Turn on persistence mode")]
    s += [P("Persistence keeps the driver loaded while no program uses the card, so the first enhancement after "
            "a quiet period does not wait for the card to wake up:")]
    s += [code("sudo systemctl enable --now nvidia-persistenced\nnvidia-smi --query-gpu=persistence_mode --format=csv,noheader")]
    s += [box("<font face='Courier'>Enabled</font>.", "check")]
    s += [sub("Offline driver installation")]
    s += [P("If the server has no internet access, download the driver packages on a connected machine running "
            "the <b>same release</b> (same Ubuntu version, or same Red Hat major and minor version and kernel) and "
            "copy them across:")]
    s += [code("""mkdir nvidia-debs && cd nvidia-debs
apt-get download $(apt-cache depends --recurse --no-recommends --no-suggests \\
  --no-conflicts --no-breaks --no-replaces --no-enhances \\
  nvidia-driver-570-server nvidia-utils-570-server | grep '^\\w' | sort -u)
# copy the folder to the server, then on the server:
sudo apt install -y ./*.deb && sudo reboot""", "Ubuntu, offline")]
    s += [code("""# on the connected machine, after adding EPEL and NVIDIA's repository as above:
mkdir nvidia-rpms && cd nvidia-rpms
sudo dnf download --resolve --alldeps nvidia-open dkms kernel-devel-$(uname -r)
# copy the folder to the server, then on the server:
sudo dnf install -y ./*.rpm && sudo reboot""", "Red Hat, offline")]
    s += [P("NVIDIA's own <font face='Courier'>.run</font> installer for the L40S (from nvidia.com, data-centre "
            "driver for Linux 64-bit) also works on both; it needs a compiler and the kernel headers on the server.", small)]

    # ------------------------------------------------------------------------------------------
    s += step(3, "Get the package onto the server")
    s += [sub("3.1 Download it")]
    s += [P("The package is built and tested by GitHub Actions, as the Windows installer is. On any PC with a "
            "browser:")]
    s += bullets(
        "<b>From a release</b> (when one is published): open the repository's <b>Releases</b> page and download "
        f"<font face='Courier'>{PKG}.tar.gz</font> and <font face='Courier'>{PKG}.tar.gz.sha256</font>. "
        "A release file can be at most 2 GB; if the package is larger it is published in parts "
        f"(<font face='Courier'>{PKG}.tar.gz.part00</font>, <font face='Courier'>.part01</font>, ...): download every part.",
        "<b>From the latest build</b>: open the repository's <b>Actions</b> tab, choose the "
        "<b>Linux server package</b> workflow, open the newest run on <b>main</b> with a green tick, and under "
        "<b>Artifacts</b> download <b>Lucida-Linux-Server</b>. You must be signed in to GitHub. It downloads "
        "as a .zip that holds the .tar.gz and its .sha256; unzip it on your PC.")
    s += [box("The package is about 2 GB, mostly NVIDIA's CUDA and cuDNN libraries. That is what lets the "
              "server work without a CUDA installation.", "note")]
    s += [sub("3.2 Copy it to the server")]
    s += [P("From the folder on your PC that holds the downloaded files (Windows PowerShell, macOS or Linux "
            "terminal; on Windows, WinSCP works too):")]
    s += [code(f"scp {PKG}.tar.gz* <your user>@<server address>:~/", "On your PC")]
    s += [P("If you downloaded parts, join them on the server first:")]
    s += [code(f"cat {PKG}.tar.gz.part* > {PKG}.tar.gz")]
    s += [sub("3.3 Check it and unpack it")]
    s += [code(f"cd ~\nsha256sum -c {PKG}.tar.gz.sha256\ntar -xzf {PKG}.tar.gz\ncd {PKG}\nls")]
    s += [box(f"<font face='Courier'>{PKG}.tar.gz: OK</font>, then the folder holds "
              "<font face='Courier'>install.sh</font>, <font face='Courier'>check-server.sh</font>, "
              "<font face='Courier'>INSTALL.txt</font>, <font face='Courier'>PACKAGE.SHA256SUMS</font>, "
              "<font face='Courier'>linux-server-install.pdf</font> (this guide) and "
              "<font face='Courier'>payload/</font>. If the check says <i>FAILED</i>, the download is incomplete "
              "or damaged: download it again. Do not install a package that fails this check.", "check")]
    s += [P("Your lab may record this SHA-256 with its validation records: "
            f"<font face='Courier'>cat {PKG}.tar.gz.sha256</font>.", small)]

    # ------------------------------------------------------------------------------------------
    s += step(4, "Check the server")
    s += [P("<font face='Courier'>check-server.sh</font> looks at everything Lucida depends on and changes "
            "nothing. Run it from the unpacked folder:")]
    s += [code("./check-server.sh")]
    s += [P("Each line is <b>OK</b>, <b>WARN</b> (Lucida works, but not as well as it could) or <b>FAIL</b> (fix it "
            "first; the installer stops on a FAIL). On a correctly prepared server:")]
    s += [code("""  OK    operating system: Red Hat Enterprise Linux 9.4 (Plow) (Red Hat family)
  OK    processor architecture: x86_64
  OK    C library: glibc 2.39
  OK    systemd is running
  OK    memory: 62 GB
  OK    processor: 32 threads (...)
  OK    free disk for case data (/var/lib): 812 GB
  OK    free disk for programs (/opt): 812 GB
  OK    graphics card on the PCI bus: 3D controller: NVIDIA Corporation AD102GL [L40S]
  OK    NVIDIA driver 570.xx.xx, supports CUDA 12.8; card NVIDIA L40S (46068 MiB)
  OK    the card is an L40S, as this package is tuned for
  OK    persistence mode is on
  OK    SELinux: Enforcing (the installer labels Lucida's folders; ...)
  OK    port 8443 is free
  OK    network addresses: 10.20.30.40
  OK    the clock is synchronised (audit-log times are right)
  All checks passed.""", "Example output")]
    s += table([["Message", "What to do"],
                ["driver ... is too old / no NVIDIA driver", "Step 2."],
                ["nouveau driver is loaded", "Step 2.3."],
                ["port 8443 is already used by ...", "Another program uses the port. Install with another, e.g. "
                 "<font face='Courier'>sudo ./install.sh --port 9443</font>."],
                ["free disk ... (at least 20 GB is needed)", "Free space, or put the case data on another disk "
                 "(step 5, <font face='Courier'>--data</font>)."],
                ["lspci is not installed", "Ubuntu <font face='Courier'>sudo apt install pciutils</font>; Red Hat "
                 "<font face='Courier'>sudo dnf install pciutils</font>. Then run the check again."],
                ["memory: under 60 GB", "Works; long video frame ranges are refused sooner (Lucida says how many frames fit)."],
                ["the clock is not synchronised", "Step 1, the orange box."],
                ["persistence mode is off", "Step 2.4."]],
               [62 * mm, 110 * mm])

    # ------------------------------------------------------------------------------------------
    s += step(5, "Install Lucida")
    s += [P("From the unpacked folder:")]
    s += [code("sudo ./install.sh")]
    s += [P("That is all most servers need. The installer:")]
    s += bullets(
        "checks every file of the package against its SHA-256 and runs the server check again;",
        "creates the system account <b>lucida</b> (no login, no password) that the service runs as, and adds you "
        "to the group <b>lucida</b> so you can read logs and copy files for import;",
        "copies the programs to <font face='Courier'>/opt/lucida</font> and links the "
        "<font face='Courier'>lucida</font> command into <font face='Courier'>/usr/local/bin</font>;",
        "writes <font face='Courier'>/etc/lucida/workbench.ini</font>, sized for this server: with 64 GB and "
        "an L40S, 4 jobs at once on the CPU and 2 on the GPU (the L40S has room for two models side by side);",
        "creates <font face='Courier'>/var/lib/lucida</font> for the case data, readable only by "
        "<b>lucida</b> and its group;",
        "installs and starts the <b>lucida</b> service (it starts at every boot and restarts if it stops), "
        "opens port 8443 in the firewall if one is active (<font face='Courier'>ufw</font> on Ubuntu, "
        "<font face='Courier'>firewalld</font> on Red Hat, where it is on by default), restores the SELinux labels "
        "on Red Hat, and creates the HTTPS certificate authority for this server;",
        "runs the acceptance self-test and shows where the models run, then prints the addresses to open "
        "and the certificate's fingerprint.")
    s += [P("It takes about five minutes, most of it the self-test. The end of the output looks like this:")]
    s += [code("""== Graphics card
Graphics card:   NVIDIA L40S, 44 GB
ONNX Runtime:    1.26.0, providers CUDAExecutionProvider, CPUExecutionProvider
AI device rule:  auto (GPU via CUDAExecutionProvider when reproducible, else CPU)
Models (loaded, checked and run twice; the GPU is used only when both runs match):
  sr.fsrcnn_x2                     gpu (CUDAExecutionProvider)
  ...
RESULT: models run on the GPU

== Done
  Open Lucida in Chrome on an examiner PC at:
      https://10.20.30.40:8443
      https://lucida-srv.lab.local:8443

  First install the certificate authority on each examiner PC (once):
      /var/lib/lucida/tls/workbench-ca.crt  (copy: /home/admin/lucida-ca.crt)
      SHA-256 3A:7F:...:C2
  The first person to open Lucida creates the administrator account.""", "Example output")]
    s += [box("If the last lines of the graphics-card part say <i>models run on the CPU</i>, Lucida works but "
              "slower; see <i>Troubleshooting</i>, <i>The models run on the CPU</i>.", "note")]
    s += [sub("Options")]
    s += table([["Option", "Use it when"],
                ["--data /srv/lucida", "The case data should live on a separate, larger disk. Mount the disk first "
                 "(and add it to /etc/fstab), then install with this option."],
                ["--port 9443", "Port 8443 is taken, or your network allows another. --port 443 lets examiners "
                 "type the address without a port."],
                ["--hostname lucida.lab.local,10.0.0.5", "Examiners reach the server by a DNS alias or an address "
                 "the server does not see on its own interfaces (NAT). The server's own name and addresses are "
                 "always included."],
                ["--no-sign-in", "Only for a closed test network: without sign-in, anyone who can reach the port "
                 "can use Lucida."],
                ["--local-only", "No network access at all: Lucida answers only on the server itself "
                 "(http://127.0.0.1:8443)."],
                ["--no-firewall", "You manage the firewall yourself."],
                ["--skip-selftest", "Skips the self-test (run lucida selftest later)."],
                ["-y", "Do not ask before continuing past warnings."]],
               [52 * mm, 120 * mm], mono=(0,))
    s += [box("<b>Red Hat and SELinux.</b> Leave SELinux in <i>Enforcing</i> mode. Lucida runs as an ordinary "
              "system service; the installer gives its folders the standard labels for programs (/opt), state "
              "(/var/lib) and settings (/etc). If you choose a data folder elsewhere with "
              "<font face='Courier'>--data</font>, the installer labels it as well.", "note")]
    s += [box("Sign-in is on by default, as it should be for a server on a shared network. Every action is "
              "recorded in the audit log under the signed-in person's name, and HTTPS keeps passwords off "
              "the network in clear.", "warn")]

    # ------------------------------------------------------------------------------------------
    s += step(6, "Check that the service runs")
    s += [code("""systemctl status lucida          # should say: active (running)
journalctl -u lucida -n 30       # the last lines of its log
lucida gpu                       # where the models run""")]
    s += [box("<font face='Courier'>Active: active (running)</font>, and in the log lines like "
              "<font face='Courier'>Address: https://127.0.0.1:8443</font>, "
              "<font face='Courier'>Lab network: https://...</font> and "
              "<font face='Courier'>Uvicorn running on https://0.0.0.0:8443</font>. "
              "<font face='Courier'>lucida gpu</font> ends with <i>RESULT: models run on the GPU</i>.", "check")]
    s += [P("While a model runs, <font face='Courier'>nvidia-smi</font> lists a <i>python3</i> process using the "
            "card. To watch it: <font face='Courier'>watch -n 1 nvidia-smi</font> (Ctrl+C to stop).")]

    # ------------------------------------------------------------------------------------------
    s += step(7, "Trust the server's certificate on each examiner PC")
    s += [P("Lucida made its own small certificate authority (CA) for this server, with no internet. Chrome trusts "
            "Lucida once each PC trusts that CA. You do this once per PC; the CA is valid for ten years and survives "
            "upgrades, and the server certificate it signs is renewed automatically when the server's names or "
            "addresses change.")]
    s += [sub("7.1 Copy the CA certificate to the PC")]
    s += [P("The installer put a copy in your home folder on the server. On the PC:")]
    s += [code("scp <your user>@<server address>:lucida-ca.crt .", "On the PC (PowerShell, or Terminal on macOS)")]
    s += [P("Check that the file arrived unchanged: the two SHA-256 values must be the same.")]
    s += [code("sha256sum ~/lucida-ca.crt", "On the server")]
    s += [code("certutil -hashfile lucida-ca.crt SHA256", "On a Windows PC")]
    s += [code("shasum -a 256 lucida-ca.crt", "On a Mac")]
    s += [box("Install only a CA file whose SHA-256 matches. A CA in the trusted-root store can vouch for any "
              "web site to that PC, so it must be the one from your server. Lucida uses its CA only to sign "
              "this server's certificate, and its private key never leaves the server. Keep <font face='Courier'>/var/lib/lucida/tls/workbench-ca.key</font> private; it never needs to "
              "leave the server.", "warn")]
    s += [sub("7.2 Windows (Chrome and Edge use the Windows certificate store)")]
    s += [P("<b>With the mouse:</b> double-click <font face='Courier'>lucida-ca.crt</font> &gt; <b>Install Certificate...</b> "
            "&gt; <b>Local Machine</b> &gt; <b>Next</b> (approve the administrator prompt) &gt; "
            "<b>Place all certificates in the following store</b> &gt; <b>Browse...</b> &gt; "
            "<b>Trusted Root Certification Authorities</b> &gt; <b>OK</b> &gt; <b>Next</b> &gt; <b>Finish</b>.")]
    s += [P("<b>Or in one command</b>, in a PowerShell or Command Prompt opened with <b>Run as administrator</b>:")]
    s += [code("certutil -addstore -f Root lucida-ca.crt", "On a Windows PC, as administrator")]
    s += [P("Then close every Chrome window (and check no Chrome icon remains near the clock) and open Chrome again. "
            "For many PCs, a domain administrator can push the CA with Group Policy: <i>Computer Configuration &gt; "
            "Policies &gt; Windows Settings &gt; Security Settings &gt; Public Key Policies &gt; Trusted Root "
            "Certification Authorities &gt; Import</i>.")]
    s += [sub("7.3 macOS")]
    s += [code("sudo security add-trusted-cert -d -r trustRoot \\\n  -k /Library/Keychains/System.keychain lucida-ca.crt",
               "On a Mac (Terminal)")]
    s += [P("Or double-click the file, add it to the <b>System</b> keychain, open it in Keychain Access, expand "
            "<b>Trust</b> and set <b>When using this certificate</b> to <b>Always Trust</b>. Restart Chrome.")]
    s += [sub("7.4 Linux desktop")]
    s += [code("sudo apt install -y libnss3-tools\ncertutil -d sql:$HOME/.pki/nssdb -A -t \"C,,\" -n \"Lucida CA\" -i lucida-ca.crt",
               "On a Linux PC")]
    s += [P("Or in Chrome: <b>Settings &gt; Privacy and security &gt; Security &gt; Manage certificates</b>, then "
            "import the file as a trusted authority for web sites. Restart Chrome.")]

    # ------------------------------------------------------------------------------------------
    s += step(8, "Open Lucida in Chrome and create the administrator")
    s += [P("On the examiner PC, open Chrome and go to the address the installer printed, for example:")]
    s += [code("https://10.20.30.40:8443", "In Chrome's address bar")]
    s += [box("The Lucida sign-in screen, and a closed padlock (or the <i>tune</i> icon) at the left of the address "
              "bar with no warning. Clicking it shows <i>Connection is secure</i>.", "check")]
    s += [P("If Chrome shows <i>Your connection is not private</i>, do not click through it: the CA is not trusted "
            "yet on this PC, or the address is not in the certificate. See <i>Troubleshooting</i>.")]
    s += [sub("8.1 The first sign-in")]
    s += [P("The <b>first person</b> to open Lucida on a new installation creates the <b>administrator</b>: user name, "
            "full name and a password of at least 12 characters. Do this straight after installing, before you "
            "tell examiners the address.")]
    s += [sub("8.2 Add the examiners")]
    s += bullets(
        "Click your name at the top right &gt; <b>Users...</b> and add each person with a role: <b>admin</b> "
        "(everything, user management), <b>examiner</b> (works on cases they are a member of), <b>reviewer</b> "
        "(reads member cases, records review decisions) or <b>viewer</b> (reads member cases only).",
        "Create a <b>second administrator</b> account and keep its password in your lab's safe. An administrator "
        "resets other people's passwords; with two, losing one password never locks you out.",
        "In each case, <b>Case &gt; Who can open this case</b> lists its members. A case an examiner creates is "
        "theirs automatically.")
    s += [P("Sessions end after 30 minutes without use and after 12 hours at most. Five wrong passwords lock an "
            "account for 15 minutes.")]
    s += [sub("8.3 Tip: bookmark it, or install it as an app")]
    s += [P("Bookmark the address. In Chrome you can also choose the three-dot menu: <b>Cast, save and share &gt; Install page "
            "as app</b> (older versions: <b>More tools &gt; Create shortcut &gt; Open as window</b>) so Lucida opens "
            "in its own window from the Start menu or the Dock.")]

    # ------------------------------------------------------------------------------------------
    s += step(9, "Run and keep the acceptance self-test")
    s += [P("The installer already ran it. Run it again after every upgrade, driver update or hardware change, "
            "and keep its log with your lab's validation records:")]
    s += [code("lucida selftest\nls /var/lib/lucida/logs/")]
    s += [P("It re-hashes every installed file against the package's SHA256SUMS, checks FFmpeg, re-hashes every "
            "model, checks ONNX Runtime gives bit-identical output on this machine and names the device the "
            "models run on, then runs a small case end to end (ingest, render, reproduce, triage, measure, "
            "review, PDF report) in a temporary folder. Your case data is not touched.")]
    s += [box("A list of <font face='Courier'>PASS</font> lines ending with <i>All checks passed.</i> and "
              "<i>Log saved to /var/lib/lucida/logs/selftest-&lt;date&gt;.txt</i>. The line "
              "<i>FSRCNN x2 reproducible on gpu (CUDAExecutionProvider)</i> confirms the L40S is used.", "check")]
    s += [P("Other checks you can run at any time:")]
    s += table([["Command", "What it checks"],
                ["lucida verify-install", "Every installed program file against the package's SHA-256 list."],
                ["lucida verify", "Re-hashes every original in the evidence store and checks the audit log's hash chain."],
                ["lucida gpu", "The card, the driver and where each model runs."],
                ["lucida check", "The server check from step 4."],
                ["lucida version", "Version, build date, commit and the hashes of the bundled components."]],
               [48 * mm, 124 * mm], mono=(0,))

    # ------------------------------------------------------------------------------------------
    s += section("Everyday operation")
    s += [sub("Start, stop, restart, log")]
    s += [code("""sudo systemctl stop lucida        # stop (examiners lose their connection)
sudo systemctl start lucida       # start
sudo systemctl restart lucida     # after changing settings
systemctl status lucida           # running or not
journalctl -u lucida -f           # follow the log (Ctrl+C to stop)
journalctl -u lucida --since today""")]
    s += [P("A job that was running when Lucida stopped is marked failed and must be started again; jobs still "
            "waiting resume. Tell examiners before a restart.")]
    s += [sub("Settings")]
    s += [P("Edit <font face='Courier'>sudo nano /etc/lucida/workbench.ini</font>, then "
            "<font face='Courier'>sudo systemctl restart lucida</font>. The most useful keys:")]
    s += table([["Key", "Meaning"],
                ["port", "The HTTPS port (also open it in the firewall: Ubuntu sudo ufw allow &lt;port&gt;/tcp; Red Hat "
                 "sudo firewall-cmd --permanent --add-port=&lt;port&gt;/tcp, then --reload)."],
                ["network", "lab = HTTPS for other PCs; local = this server only."],
                ["auth", "accounts = sign-in with roles. Remove the line only on a closed test network."],
                ["allowed_hosts", "Extra names or addresses examiners type, comma-separated (also added to the certificate)."],
                ["job_workers / gpu_jobs", "How many jobs run at once on the CPU / on the GPU (4 / 2 on this server)."],
                ["ai_device", "auto (GPU when reproducible), gpu or cpu."],
                ["session_minutes", "Minutes without use before a session ends (default 30)."],
                ["tls_cert / tls_key", "Use your lab's own certificate instead of Lucida's CA (PEM files readable by group lucida)."],
                ["c2pa_trust_anchors", "PEM file of C2PA signers your lab trusts."],
                ["data_dir / import_dir", "Case data and import folders. If you change them, run the installer again so "
                 "the service may write there."]],
               [40 * mm, 132 * mm], mono=(0,))
    s += [sub("Bringing in evidence")]
    s += [P("Examiners upload files through Chrome; each file is hashed (SHA-256 and MD5) as it arrives and stored "
            "read-only. For large exports, copy them to the import folder on the server and ingest them from "
            "there; members of the group <b>lucida</b> may write to it:")]
    s += [code("cp -r /media/usb/DVR-export /var/lib/lucida/import/\nlucida batch scan /var/lib/lucida/import/DVR-export")]
    s += [sub("Backups")]
    s += [P("A backup is consistent while Lucida runs and lists every file's SHA-256. Keep backups on separate media "
            "and test a restore now and then:")]
    s += [code("""# the backup folder must be writable by the lucida account
sudo install -d -o lucida -g lucida /mnt/backup/lucida
LUCIDA_BACKUP_PASSWORD='<a long password>' lucida backup \\
  --output /mnt/backup/lucida/lucida-$(date +%F).backup --encrypt
# test a restore into an empty folder
# (it checks every file, re-hashes the originals and verifies the audit chain):
sudo install -d -o lucida -g lucida /mnt/backup/restore-test
lucida restore /mnt/backup/lucida/lucida-<date>.backup \\
  --target /mnt/backup/restore-test""")]
    s += [P("Note: the <font face='Courier'>lucida</font> command runs as the <b>lucida</b> account (it switches with "
            "sudo), so the folders you give it must be readable or writable by that account.", small)]
    s += [sub("Upgrading")]
    s += [P("Get the new package (step 3), unpack it and run its installer. It stops the service, replaces the "
            "programs, keeps <font face='Courier'>/etc/lucida/workbench.ini</font>, the case data and the certificate "
            "authority (so examiner PCs need nothing new), starts the service and runs the self-test.")]
    s += [code(f"cd ~/{PKG.replace(VERSION, '<new version>')}\nsudo ./install.sh")]
    s += [P("A driver or kernel upgrade (<font face='Courier'>sudo apt upgrade</font> or "
            "<font face='Courier'>sudo dnf upgrade</font>, followed by a reboot) needs nothing from Lucida; run <font face='Courier'>lucida gpu</font> afterwards to confirm the GPU is still used.")]
    s += [sub("Uninstalling")]
    s += [code("""# remove the programs and the service; KEEP the case data and settings:
sudo /opt/lucida/uninstall.sh
# also delete the case data, the settings and the lucida account:
sudo /opt/lucida/uninstall.sh --purge""")]
    s += [box("<font face='Courier'>--purge</font> deletes all evidence, derived files, the audit log and the accounts. "
              "It asks you to type DELETE and then to confirm. Back up first.", "warn")]

    # ------------------------------------------------------------------------------------------
    s += section("Troubleshooting")
    s += [P("Start with these three commands; together they answer most questions:")]
    s += [code("systemctl status lucida\njournalctl -u lucida -n 80 --no-pager\nlucida check")]
    rows = [["What you see", "Why", "What to do"],
            ["Chrome: <i>Your connection is not private</i>, NET::ERR_CERT_AUTHORITY_INVALID",
             "This PC does not trust Lucida's CA yet.",
             "Step 7 on this PC, into <b>Trusted Root Certification Authorities</b> of the <b>Local Machine</b>; then quit "
             "Chrome completely and reopen it."],
            ["Chrome: NET::ERR_CERT_COMMON_NAME_INVALID",
             "The address you typed is not in the server certificate (a DNS alias, a NAT address).",
             "Add it: in workbench.ini set <font face='Courier'>allowed_hosts = name1,10.0.0.5</font>, then "
             "<font face='Courier'>sudo systemctl restart lucida</font>. The certificate is re-issued; the CA stays the same. "
             "<font face='Courier'>lucida ca</font> lists the names it contains."],
            ["The page says <i>Invalid host header</i> (400)",
             "Lucida answers only names it knows, against DNS-rebinding attacks.",
             "As above: add the name to <font face='Courier'>allowed_hosts</font> and restart."],
            ["Chrome: ERR_CONNECTION_REFUSED",
             "Nothing listens on that port: the service is stopped, or a different port is set.",
             "<font face='Courier'>systemctl status lucida</font>; "
             "<font face='Courier'>sudo ss -ltnp | grep 8443</font>; start it, or use the port in workbench.ini."],
            ["Chrome: ERR_CONNECTION_TIMED_OUT",
             "A firewall drops the traffic: on the server, on the network, or (in a cloud) the security group.",
             "On the server, Ubuntu: <font face='Courier'>sudo ufw status</font> (allow with "
             "<font face='Courier'>sudo ufw allow 8443/tcp</font>); Red Hat: "
             "<font face='Courier'>sudo firewall-cmd --list-ports</font> (allow with "
             "<font face='Courier'>sudo firewall-cmd --permanent --add-port=8443/tcp &amp;&amp; sudo firewall-cmd --reload</font>). From the PC: "
             "<font face='Courier'>Test-NetConnection &lt;server&gt; -Port 8443</font> in PowerShell. "
             "Otherwise ask your network team to allow TCP 8443 from the examiners' network."],
            ["Chrome: ERR_SSL_PROTOCOL_ERROR, or the address starts with http://",
             "Lucida on the network speaks HTTPS only.",
             "Type <b>https://</b> in front of the address."],
            ["Service <i>activating (auto-restart)</i> or <i>failed</i>",
             "Lucida stops right after starting.",
             "<font face='Courier'>journalctl -u lucida -n 80</font> shows why. Common: the port is used by another "
             "program; the data disk is not mounted or full; workbench.ini was edited with a typo."],
            ["The models run on the CPU (<font face='Courier'>lucida gpu</font> says cpu)",
             "The driver is missing or too old, the card is in use by something that took all its memory, or the "
             "CUDA device files are missing.",
             "<font face='Courier'>nvidia-smi</font> must work (step 2) and show driver 525 or newer. Then "
             "<font face='Courier'>sudo nvidia-modprobe -u -c=0</font> and "
             "<font face='Courier'>sudo systemctl restart lucida</font>. "
             "<font face='Courier'>journalctl -u lucida | grep -i -E 'cuda|cudnn'</font> shows CUDA errors. "
             "Check that <font face='Courier'>ai_device</font> is not <i>cpu</i> in workbench.ini."],
            ["<font face='Courier'>nvidia-smi</font>: couldn't communicate with the NVIDIA driver",
             "Driver not loaded: no reboot yet, Secure Boot rejected it, or nouveau is loaded.",
             "Step 2.3."],
            ["A model is <i>not reproducible on the GPU</i>; it runs on the CPU",
             "Two GPU runs gave different bits, so Lucida chose the CPU for that model to keep results reproducible.",
             "Nothing to fix: the result is still correct. Report the driver version "
             "(<font face='Courier'>nvidia-smi</font>) with the self-test log."],
            ["Red Hat: the service fails with <i>Permission denied</i> although the files look right",
             "SELinux refused something, usually a data folder outside /var/lib that was not labelled.",
             "<font face='Courier'>sudo ausearch -m avc -ts recent</font> shows what was refused. Run "
             "<font face='Courier'>sudo restorecon -RF /opt/lucida /var/lib/lucida</font> (and your data folder), or "
             "<font face='Courier'>sudo ./install.sh</font> again, which does this."],
            ["The self-test FAILS on <i>installed files</i>",
             "A program file was changed or removed after installation.",
             "Run <font face='Courier'>sudo ./install.sh</font> again from the package; it replaces every file."],
            ["<font face='Courier'>lucida</font> asks for your password",
             "It switches to the lucida account through sudo.",
             "Type your own password. Your user must be allowed to use sudo (group <i>sudo</i> on Ubuntu, <i>wheel</i> on Red Hat)."],
            ["<i>Permission denied</i> reading files to import",
             "The lucida account cannot read your home folder.",
             "Copy the files to <font face='Courier'>/var/lib/lucida/import</font> first."],
            ["Forgot a password",
             "",
             "Another administrator resets it in <b>Users...</b> (it also ends that person's sessions). This is "
             "why step 8.2 asks for a second administrator."],
            ["Lucida is slow with long videos; <i>range would not fit</i>",
             "One frame range may use a sixth of RAM (about 10 GB on 64 GB) so the server never swaps.",
             "Work on a shorter range, as the message suggests; or raise "
             "<font face='Courier'>range_mb</font> in workbench.ini if the server has nothing else to do."],
            ["Disk full", "Evidence and derived video accumulate.",
             "<font face='Courier'>df -h /var/lib/lucida</font>. Move the case data to a larger disk: stop Lucida, copy "
             "with <font face='Courier'>sudo rsync -aHAX /var/lib/lucida/ /srv/lucida/</font>, set "
             "<font face='Courier'>data_dir</font> and <font face='Courier'>import_dir</font>, run "
             "<font face='Courier'>sudo ./install.sh</font> again (it updates the service), check, then remove the old copy."]]
    s += table(rows, [44 * mm, 50 * mm, 78 * mm])
    s += [sub("Collecting information for support")]
    s += [code("""{ lucida version; lucida check; lucida gpu; nvidia-smi
  systemctl status lucida --no-pager
  journalctl -u lucida -n 300 --no-pager; } > ~/lucida-support.txt 2>&1""")]
    s += [P("The file holds no evidence or passwords. Attach it, together with the newest log in "
            "<font face='Courier'>/var/lib/lucida/logs</font>, to your support request.")]

    # ------------------------------------------------------------------------------------------
    s += section("Quick reference")
    s += table([["Task", "Command"],
                ["Check the server", "./check-server.sh   (or: lucida check)"],
                ["Install or upgrade", "sudo ./install.sh"],
                ["Status / log", "systemctl status lucida  /  journalctl -u lucida -f"],
                ["Restart after a settings change", "sudo systemctl restart lucida"],
                ["Settings file", "/etc/lucida/workbench.ini"],
                ["Case data", "/var/lib/lucida"],
                ["CA certificate for examiner PCs", "/var/lib/lucida/tls/workbench-ca.crt  (lucida ca)"],
                ["Self-test", "lucida selftest"],
                ["Where the models run", "lucida gpu"],
                ["Check installed files", "lucida verify-install"],
                ["Check evidence and audit log", "lucida verify"],
                ["Backup", "lucida backup --output FILE --encrypt"],
                ["Uninstall (keep data)", "sudo /opt/lucida/uninstall.sh"],
                ["Address for examiners", "https://&lt;server address&gt;:8443"]],
               [58 * mm, 114 * mm], mono=(1,))
    s += [Spacer(1, 6), P("More: the user manual and the algorithm guide are in Lucida's Help page and in "
                          "/opt/lucida/docs. Deployment details for every option: /opt/lucida/docs/deployment.md.", small)]
    return s


def build(out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = Doc(str(out), pagesize=A4, leftMargin=19 * mm, rightMargin=19 * mm, topMargin=18 * mm,
              bottomMargin=22 * mm, title=f"Lucida {VERSION}: Linux server installation guide",
              author="Lucida", subject="Installing Lucida on a Linux server with an NVIDIA L40S")
    doc.multiBuild(cover() + content(), onFirstPage=on_page, onLaterPages=on_page)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "docs" / "linux-server-install.pdf"))
    build(Path(ap.parse_args().out))
    print(ap.parse_args().out)
