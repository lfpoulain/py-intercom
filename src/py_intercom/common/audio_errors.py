from __future__ import annotations

import sounddevice as sd


def _hostapi_name(dev_info: dict | None) -> str:
    """Return a device's host API name without assuming PortAudio indices are stable."""
    if not isinstance(dev_info, dict):
        return ""

    raw_hostapi = dev_info.get("hostapi")
    if isinstance(raw_hostapi, str):
        return raw_hostapi.strip()
    try:
        hostapi = sd.query_hostapis(int(raw_hostapi))
    except Exception:
        return ""
    if not isinstance(hostapi, dict):
        return ""
    return str(hostapi.get("name") or "").strip()


def shared_stream_extra_settings(dev_info: dict | None):
    """Enable Windows' shared-mode format conversion for WASAPI endpoints.

    py-intercom deliberately keeps its network/Opus clock at 48 kHz.  A
    shared WASAPI endpoint can ask the Windows audio engine to convert a
    device's current mix format while preserving that internal clock.  The
    option must not be passed to another PortAudio host API (notably WDM-KS),
    where it is invalid and no system resampler is available.
    """
    if "WASAPI" not in _hostapi_name(dev_info).upper():
        return None
    try:
        return sd.WasapiSettings(exclusive=False, auto_convert=True)
    except Exception:
        # Keep enumeration usable on non-Windows test/dev hosts and with old
        # PortAudio builds that do not expose this shared-mode option.
        return None


def _device_label(dev_info: dict | None, fallback: int | None) -> str:
    if isinstance(dev_info, dict):
        name = " ".join(str(dev_info.get("name") or "").split())
        if name:
            return name
    if fallback is None:
        return "(défaut)"
    return f"#{int(fallback)}"


def looks_like_bluetooth(dev_info: dict | None) -> bool:
    if not isinstance(dev_info, dict):
        return False
    name = str(dev_info.get("name") or "").lower()
    needles = (
        "bluetooth",
        "bthhfenum",
        "hands-free",
        "hands free",
        "mains-libres",
        "mains libres",
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
    dev_info: dict | None,
    device: int | None,
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
            "Windows bascule tout le casque en profil mains-libres/HFP "
            "(écoute et capture mono 8/16 kHz).\n"
            "Solutions :\n"
            "  • pour utiliser le micro ET le casque, choisir les deux endpoints "
            "mains-libres/HFP du même casque via WASAPI partagé, DirectSound ou MME,\n"
            "  • utiliser un casque USB ou jack (recommandé pour l'intercom),\n"
            "  • pour conserver la sortie stéréo A2DP, utiliser un autre micro ; "
            "dans ce cas seulement, le service « Téléphonie mains libres » peut "
            "être désactivé dans Windows.\n"
            "Une sortie A2DP et le micro HFP du même casque ne constituent pas "
            "une paire simultanée fiable."
        )

    return f"{pa_hint}{bt_hint}\n\nDétail technique : {raw}"
