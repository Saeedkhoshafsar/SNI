"""Profile editor dialog — paste a share link, review the parsed fields, edit
if needed, then add.

This is the v2rayN-style "smart import" the user asked for: instead of typing
``127.0.0.1:40443`` by hand, they paste a ``vless://`` / ``vmess://`` /
``trojan://`` / ``ss://`` link; :mod:`core.share_link` decodes every field, and
this dialog presents them in an editable form pre-filled with the parsed values.
On accept it rebuilds a validated :class:`~core.profile.Profile`.

The dialog is **framework-thin**: it knows the :class:`Profile` dataclass and
nothing about the engine, so it is unit-testable headless (offscreen Qt).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from core.profile import Profile, PROTOCOLS, TRANSPORTS, SECURITIES


# Fields shown per section. Each entry: (attr, label, widget-kind).
#   kind: "line" | "int" | "combo:<enum>" | "bool"
_BASIC = [
    ("remark", "نام نمایشی", "line"),
    ("protocol", "پروتکل", "combo:proto"),
    ("address", "آدرس سرور", "line"),
    ("port", "پورت", "int"),
]
_CREDENTIALS = [
    ("uuid", "UUID", "line"),
    ("password", "رمز عبور", "line"),
    ("method", "روش رمزنگاری (SS)", "line"),
    ("alter_id", "Alter ID (VMess)", "int"),
    ("flow", "Flow (VLESS)", "line"),
]
_TRANSPORT = [
    ("transport", "ترنسپورت", "combo:transport"),
    ("host", "Host هدر", "line"),
    ("path", "مسیر / serviceName", "line"),
    ("header_type", "نوع هدر", "line"),
    ("mode", "حالت XHTTP (auto/packet-up/…)", "line"),
]
_SECURITY = [
    ("security", "امنیت", "combo:security"),
    ("sni", "SNI", "line"),
    ("alpn", "ALPN", "line"),
    ("fingerprint", "اثرانگشت (uTLS)", "line"),
    ("public_key", "Public Key (Reality)", "line"),
    ("short_id", "Short ID (Reality)", "line"),
]


class ProfileDialog(QDialog):
    """Editable form for one :class:`Profile`. Returns the edited profile."""

    def __init__(self, profile: Profile, parent: QWidget | None = None,
                 *, title: str = "ویرایش پروفایل"):
        super().__init__(parent)
        self._source = profile
        self._widgets: dict[str, QWidget] = {}
        self.setWindowTitle(title)
        self.setObjectName("ProfileDialog")
        self.setModal(True)
        self.setMinimumWidth(440)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)

        head = QLabel(title)
        head.setObjectName("H1")
        root.addWidget(head)
        hint = QLabel("مقادیر از روی لینک پر شده‌اند — در صورت نیاز ویرایش کنید")
        hint.setObjectName("Muted")
        root.addWidget(hint)

        # scrollable form so long profiles still fit small screens
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setObjectName("DialogScroll")
        inner = QWidget()
        form = QFormLayout(inner)
        form.setContentsMargins(2, 6, 2, 6)
        form.setSpacing(9)
        form.setLabelAlignment(Qt.AlignRight)

        for section, fields in (
            ("اطلاعات پایه", _BASIC),
            ("اعتبارنامه", _CREDENTIALS),
            ("ترنسپورت", _TRANSPORT),
            ("امنیت / TLS", _SECURITY),
        ):
            sec = QLabel(section)
            sec.setObjectName("H2")
            form.addRow(sec)
            for attr, label, kind in fields:
                w = self._make_widget(attr, kind)
                self._widgets[attr] = w
                form.addRow(label, w)

        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        # error line (validation feedback)
        self._err = QLabel("")
        self._err.setObjectName("Faint")
        self._err.setWordWrap(True)
        self._err.setProperty("class", "DialogError")
        root.addWidget(self._err)

        # buttons
        btns = QHBoxLayout()
        btns.addStretch(1)
        self.btn_cancel = QPushButton("انصراف")
        self.btn_cancel.setObjectName("Ghost")
        self.btn_ok = QPushButton("افزودن")
        self.btn_ok.setObjectName("Primary")
        btns.addWidget(self.btn_cancel)
        btns.addWidget(self.btn_ok)
        root.addLayout(btns)

        self.btn_cancel.clicked.connect(self.reject)
        self.btn_ok.clicked.connect(self._on_accept)

        self._load(profile)

    # ------------------------------------------------------------------ widgets
    def _make_widget(self, attr: str, kind: str) -> QWidget:
        if kind == "int":
            w = QSpinBox()
            w.setRange(0, 65535)
            return w
        if kind.startswith("combo:"):
            w = QComboBox()
            which = kind.split(":", 1)[1]
            items = {
                "proto": PROTOCOLS,
                "transport": TRANSPORTS,
                "security": SECURITIES,
            }[which]
            w.addItems(list(items))
            w.setEditable(False)
            return w
        w = QLineEdit()
        return w

    # ------------------------------------------------------------------ load/save
    def _load(self, p: Profile) -> None:
        for attr, w in self._widgets.items():
            val = getattr(p, attr, "")
            if isinstance(w, QSpinBox):
                try:
                    w.setValue(int(val or 0))
                except (TypeError, ValueError):
                    w.setValue(0)
            elif isinstance(w, QComboBox):
                i = w.findText(str(val))
                w.setCurrentIndex(i if i >= 0 else 0)
            else:  # QLineEdit
                w.setText(str(val))

    def collect(self) -> Profile:
        """Build a Profile from the current widget values (preserves ``raw``/``extra``)."""
        data = self._source.to_dict()
        for attr, w in self._widgets.items():
            if isinstance(w, QSpinBox):
                data[attr] = w.value()
            elif isinstance(w, QComboBox):
                data[attr] = w.currentText()
            else:
                data[attr] = w.text().strip()
        return Profile.from_dict(data)

    # ------------------------------------------------------------------ accept
    def _on_accept(self) -> None:
        prof = self.collect()
        errs = prof.validate()
        if errs:
            self._err.setText("؛ ".join(errs))
            self._err.setStyleSheet("color:#ff6b81;")
            return
        self._result = prof
        self.accept()

    @property
    def result_profile(self) -> Profile:
        return getattr(self, "_result", self.collect())
