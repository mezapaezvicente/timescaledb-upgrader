"""A StatefulSet that a Helm chart owns, and what to tell its operator.

``apply`` changes the StatefulSet's image with ``kubectl set image``. The
chart's values still name the old image, so the next ``helm upgrade`` puts it
back; after a PostgreSQL major upgrade that is an old-major image on a
new-major data directory. Measured on kind (2026-11-15, pg15 image on an
upgraded pg16 directory, under Patroni): each restarted pod's PostgreSQL
refuses the directory ("database files are incompatible with server") and
the pod never becomes ready; nothing on the disk changes, and setting the new
image back starts it again. Nothing here refuses:
preflight reports the ownership, and the runbook, the script and ``apply``'s
report end with the values to change.

Helm marks what it manages with the ``app.kubernetes.io/managed-by: Helm``
label and the ``meta.helm.sh/release-name`` annotation (Helm 3); either one
counts.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

__all__ = ["HELM_HEADING", "helm_owner", "split_image", "values_note"]

HELM_HEADING = "before your next `helm upgrade`"


def helm_owner(sts: Dict[str, Any]) -> Optional[str]:
    """The Helm release that manages the StatefulSet, ``"Helm"`` when only the
    label says so, or None when nothing marks it as Helm's."""
    meta = sts.get("metadata") or {}
    release = (meta.get("annotations") or {}).get("meta.helm.sh/release-name")
    if release:
        return release
    if (meta.get("labels") or {}).get("app.kubernetes.io/managed-by") == "Helm":
        return "Helm"
    return None


def split_image(reference: str) -> Tuple[str, Optional[str], Optional[str]]:
    """``(repository, tag, digest)`` of an image reference. A colon before the
    last slash belongs to a registry's port, not to a tag."""
    rest, _, digest = reference.partition("@")
    head, slash, last = rest.rpartition("/")
    name, colon, tag = last.partition(":")
    return head + slash + name, (tag if colon else None), (digest or None)


def values_note(image: str, release: Optional[str] = None, major: bool = False,
                statefulset: Optional[str] = None) -> List[str]:
    """The lines that end a runbook, a script or a report. ``release`` None:
    nobody looked (``plan`` is offline), so the note is conditional."""
    repository, tag, digest = split_image(image)
    if release is None:
        lead = "if a Helm chart manages this StatefulSet"
    elif release == "Helm":
        lead = "statefulset/{0} is managed by Helm".format(statefulset)
    else:
        lead = "statefulset/{0} is managed by Helm release {1}".format(statefulset, release)
    lines = ["{0} ({1}):".format(HELM_HEADING, lead),
             "  set the image repository in the chart's values to {0}".format(repository)]
    if tag:
        lines.append("  and the tag to {0}{1}".format(
            tag, " (digest {0}, if the chart takes one)".format(digest) if digest else ""))
    elif digest:
        lines.append("  and the digest to {0}".format(digest))
    lines.append("  otherwise the next `helm upgrade` puts the old image back{0}".format(
        "; after a PostgreSQL major upgrade, every pod restarted on it will not start "
        "(PostgreSQL refuses the upgraded data directory), though its data is not touched "
        "and setting the new image back starts it again" if major else ""))
    return lines
