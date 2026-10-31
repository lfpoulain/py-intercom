"""The question asked once per PC: is it the Régie or a Poste?"""

from __future__ import annotations

from PySide6 import QtWidgets

_CHOICES = (
    (
        "server",
        "RÉGIE",
        "Le PC central. Il relie les postes, mélange les voix\net envoie le son vers les sorties. Un seul par installation.",
    ),
    (
        "client",
        "POSTE",
        "Un opérateur avec micro et casque. Il parle\net écoute les autres. Un par personne.",
    ),
)


class RoleDialog(QtWidgets.QDialog):
    def __init__(self, remembered: str | None = None, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("py-intercom")
        self.role: str | None = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)

        title = QtWidgets.QLabel("Que fait ce PC ?")
        title.setProperty("role", "title")
        layout.addWidget(title)

        self.buttons: dict[str, QtWidgets.QPushButton] = {}
        for role, name, description in _CHOICES:
            button = QtWidgets.QPushButton(f"{name}\n{description}")
            button.setProperty("class", "primary" if role == remembered else "secondary")
            button.setMinimumHeight(84)
            button.clicked.connect(lambda _checked=False, r=role: self._pick(r))
            self.buttons[role] = button
            layout.addWidget(button)

        self._remember = QtWidgets.QCheckBox("Toujours ouvrir ce rôle sur ce PC")
        self._remember.setChecked(remembered is not None)
        layout.addWidget(self._remember)

        hint = QtWidgets.QLabel("Pour revoir cette question, maintenir Maj en lançant py-intercom.")
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

    @property
    def remember(self) -> bool:
        return self._remember.isChecked()

    def _pick(self, role: str) -> None:
        self.role = role
        self.accept()
