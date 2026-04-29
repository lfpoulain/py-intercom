from __future__ import annotations
from typing import Optional


def _device_label(dev_info: Optional[dict], fallback: Optional[int]) -> str:
    if isinstance(dev_info, dict):
        name = str(dev_info.get("name") or "").strip()
        if name:
            return name
    if fallback is None:
        return "(défaut)"
    return f"#{int(fallback)}"


def looks_like_bluetooth(dev_info: Optional[dict]) -> bool:
    if not isinstance(dev_info, dict):
        return False
    name = str(dev_info.get("name") or "").lower()
    needles = (
        "bluetooth",
        "hands-free",
        "hands free",
        "headset",
        "hfp",
        "airpods",
        "wh-",
        "wf-",
        "bose",
        "jabra",
        "baseus",
        "bowie",
        "buds",
        "jbl",
    )
    return any(n in name for n in needles)


def friendly_audio_open_error(
    err: Exception,
    *,
    kind: str,
    dev_info: Optional[dict],
    device: Optional[int],
    sr: int,
    ch: int,
) -> str:
    """Translate a sounddevice/PortAudio error into a clear, actionable message.

    kind: "input" or "output"
    """
    raw = str(err)
    label = _device_label(dev_info, device)
    is_bt = looks_like_bluetooth(dev_info)
    role = "micro" if kind == "input" else "sortie"

    lower = raw.lower()
    pa_hint = ""

    if "insufficient memory" in lower or "paerrorcode -9992" in lower or "-9992" in lower:
        pa_hint = (
            f"Le périphérique « {label} » ne peut pas être ouvert en {sr} Hz mono.\n"
            "PortAudio renvoie « Insufficient memory », ce qui sur Windows traduit "
            "presque toujours un format non supporté par le pilote (WASAPI), "
            "pas un vrai manque de mémoire."
        )
    elif "invalid sample rate" in lower or "-9997" in lower:
        pa_hint = (
            f"Le périphérique « {label} » ne supporte pas la fréquence d'échantillonnage "
            f"{sr} Hz requise par py-intercom."
        )
    elif "invalid channel count" in lower or "-9998" in lower:
        pa_hint = (
            f"Le périphérique « {label} » ne supporte pas {ch} canal en {role}."
        )
    elif "invalid device" in lower or "-9996" in lower:
        pa_hint = f"Le périphérique de {role} sélectionné est introuvable ou indisponible."
    elif "device unavailable" in lower or "unanticipated host error" in lower or "-9999" in lower:
        pa_hint = (
            f"Le périphérique « {label} » est indisponible (déjà utilisé par une autre "
            "application, déconnecté, ou en cours de bascule de profil)."
        )

    if not pa_hint:
        pa_hint = f"Impossible d'ouvrir le périphérique de {role} « {label} » en {sr} Hz mono."

    bt_hint = ""
    if is_bt:
        bt_hint = (
            "\n\nCe périphérique semble être un casque Bluetooth. "
            "En Bluetooth classique, dès que le micro du casque est ouvert, "
            "Windows bascule tout le casque en profil HFP (mono 8/16 kHz), "
            "ce qui empêche la sortie de fonctionner en 48 kHz.\n"
            "Solutions :\n"
            "  • utiliser un casque USB ou jack (recommandé pour l'intercom),\n"
            "  • ou utiliser le casque BT uniquement en sortie ET un autre micro,\n"
            "  • ou désactiver le service « Téléphonie mains libres » du casque "
            "dans les paramètres Bluetooth Windows pour le forcer en A2DP."
        )

    return f"{pa_hint}{bt_hint}\n\nDétail technique : {raw}"
