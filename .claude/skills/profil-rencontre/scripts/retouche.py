#!/usr/bin/env python3
"""Retouche naturelle de photos pour profil de rencontre.

Corrige ce qui gâche une photo sans changer la personne : orientation,
balance des blancs, exposition, contraste, netteté, cadrage au format de
l'appli. Supprime les métadonnées (dont la position GPS) à l'export.
Ne touche jamais au visage ni au corps.

Usage :
  python3 retouche.py photo.jpg [autres.jpg ...] --out retouchees/
  python3 retouche.py photo.jpg --analyse            # diagnostic seul, aucun fichier écrit
  python3 retouche.py photo.jpg --format 1:1 --focus 0.5,0.35 --compare

Dépendance : Pillow (pip install pillow).
"""

import argparse
import math
import sys
from pathlib import Path

try:
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageStat
except ImportError:
    sys.exit("Pillow manque : pip install pillow")

FORMATS = {"4:5": (4, 5), "1:1": (1, 1), "3:4": (3, 4), "9:16": (9, 16), "aucun": None}
TAILLE_MAX = 2160  # côté long à l'export, assez pour toutes les applis


def luminance(img):
    return ImageStat.Stat(img.convert("L")).mean[0]


def nettete(img):
    """Énergie des contours sur une version réduite : plus c'est haut, plus c'est net."""
    petit = img.convert("L")
    petit.thumbnail((800, 800))
    return ImageStat.Stat(petit.filter(ImageFilter.FIND_EDGES)).var[0]


def dominante(img):
    """Écart relatif rouge/bleu par rapport au vert (gray world)."""
    r, g, b = ImageStat.Stat(img.convert("RGB")).mean
    g = max(g, 1)
    return (r - g) / g, (b - g) / g


def diagnostic(img):
    lum = luminance(img)
    net = nettete(img)
    dr, db = dominante(img)
    w, h = img.size
    alertes = []
    if min(w, h) < 1000:
        alertes.append(f"résolution faible ({w}x{h}) : vise au moins 1080 px de large")
    if lum < 85:
        alertes.append(f"trop sombre (luminance {lum:.0f}/255) : la retouche aide, une photo de jour est mieux")
    elif lum > 190:
        alertes.append(f"surexposée (luminance {lum:.0f}/255)")
    if net < 120:
        alertes.append(f"floue (netteté {net:.0f}) : la retouche ne rattrape pas un vrai flou, reprends la photo")
    elif net < 250:
        alertes.append(f"netteté moyenne ({net:.0f})")
    if dr > 0.12:
        alertes.append("dominante orange/jaune (lumière artificielle)")
    if db > 0.12:
        alertes.append("dominante bleue (ombre ou écran)")
    return {"taille": f"{w}x{h}", "luminance": round(lum), "nettete": round(net), "alertes": alertes}


def balance_blancs(img, force):
    """Gray world plafonné : corrige une dominante sans dénaturer l'ambiance."""
    r, g, b = ImageStat.Stat(img).mean
    moy = (r + g + b) / 3
    plafond = 0.10 * force
    gains = [min(max(moy / max(c, 1), 1 - plafond), 1 + plafond) for c in (r, g, b)]
    canaux = [c.point(lambda v, k=k: min(255, int(v * k + 0.5))) for c, k in zip(img.split(), gains)]
    return Image.merge("RGB", canaux)


def tons(img, force):
    """Niveaux et exposition sur la luminance seule : les couleurs ne sont pas amplifiées."""
    y, cb, cr = img.convert("YCbCr").split()
    hist = y.histogram()
    total = sum(hist)
    coupe = total * 0.005

    def percentile(depuis_haut):
        acc = 0
        rng = range(255, -1, -1) if depuis_haut else range(256)
        for i in rng:
            acc += hist[i]
            if acc > coupe:
                return i
        return 255 if depuis_haut else 0

    bas, haut = percentile(False), percentile(True)
    # étirement plafonné : une photo terne gagne du contraste sans devenir criarde
    gain = min(255 / max(haut - bas, 1), 1 + 0.35 * force)
    centre = (bas + haut) / 2
    niveaux = [min(255, max(0, (i - centre) * gain + 127.5)) for i in range(256)]

    moyenne = sum(niveaux[i] * n for i, n in enumerate(hist)) / total
    gamma = 1.0
    if moyenne < 115 or moyenne > 170:
        # gamma qui ramène les tons moyens vers 128, plafonné
        gamma = math.log(128 / 255) / math.log(min(max(moyenne, 5), 250) / 255)
        gamma = min(max(1 + (gamma - 1) * 0.6 * force, 0.65), 1.4)
    lut = [min(255, int(255 * (v / 255) ** gamma + 0.5)) for v in niveaux]
    return Image.merge("YCbCr", (y.point(lut), cb, cr)).convert("RGB")


def recadrer(img, ratio, focus):
    if ratio is None:
        return img
    w, h = img.size
    rw, rh = ratio
    if w / h > rw / rh:  # trop large : on coupe les côtés
        nw, nh = int(h * rw / rh), h
    else:  # trop haut : on coupe haut et bas
        nw, nh = w, int(w * rh / rw)
    fx, fy = focus
    x = min(max(int(fx * w - nw / 2), 0), w - nw)
    y = min(max(int(fy * h - nh / 2), 0), h - nh)
    return img.crop((x, y, x + nw, y + nh))


def retoucher(img, force, ratio, focus):
    img = ImageOps.exif_transpose(img).convert("RGB")
    img = balance_blancs(img, force)
    img = tons(img, force)
    img = ImageEnhance.Color(img).enhance(1 + 0.05 * force)
    img = recadrer(img, ratio, focus)
    if max(img.size) > TAILLE_MAX:
        img.thumbnail((TAILLE_MAX, TAILLE_MAX), Image.LANCZOS)
    img = img.filter(ImageFilter.UnsharpMask(radius=1.6, percent=int(55 * force), threshold=3))
    return img


def avant_apres(avant, apres):
    avant = ImageOps.exif_transpose(avant).convert("RGB")
    h = 900
    a = avant.resize((int(avant.width * h / avant.height), h), Image.LANCZOS)
    b = apres.resize((int(apres.width * h / apres.height), h), Image.LANCZOS)
    planche = Image.new("RGB", (a.width + b.width + 20, h), (255, 255, 255))
    planche.paste(a, (0, 0))
    planche.paste(b, (a.width + 20, 0))
    return planche


def main():
    p = argparse.ArgumentParser(description="Retouche naturelle de photos de profil.")
    p.add_argument("photos", nargs="+", type=Path)
    p.add_argument("--out", type=Path, default=Path("retouchees"), help="dossier de sortie (défaut : retouchees/)")
    p.add_argument("--format", choices=FORMATS, default="4:5", help="cadrage (défaut 4:5, format des applis)")
    p.add_argument("--focus", default="0.5,0.4", help="centre du cadrage en fractions x,y (défaut 0.5,0.4 : visage dans le tiers haut)")
    p.add_argument("--force", choices=["douce", "normale"], default="normale")
    p.add_argument("--analyse", action="store_true", help="diagnostic seul, aucun fichier écrit")
    p.add_argument("--compare", action="store_true", help="écrit aussi une planche avant/après")
    args = p.parse_args()

    force = 0.6 if args.force == "douce" else 1.0
    try:
        focus = tuple(float(v) for v in args.focus.split(","))
        assert len(focus) == 2 and all(0 <= v <= 1 for v in focus)
    except (ValueError, AssertionError):
        sys.exit("--focus attend deux nombres entre 0 et 1, par exemple 0.5,0.35")

    if not args.analyse:
        args.out.mkdir(parents=True, exist_ok=True)

    code = 0
    for chemin in args.photos:
        try:
            src = Image.open(chemin)
            src.load()
        except Exception as e:  # fichier illisible : on continue avec les autres
            print(f"[{chemin}] illisible : {e}")
            code = 1
            continue
        diag = diagnostic(ImageOps.exif_transpose(src).convert("RGB"))
        print(f"[{chemin.name}] {diag['taille']} · luminance {diag['luminance']} · netteté {diag['nettete']}")
        for a in diag["alertes"]:
            print(f"  ! {a}")
        if args.analyse:
            continue
        res = retoucher(src, force, FORMATS[args.format], focus)
        cible = args.out / f"{chemin.stem}-retouche.jpg"
        res.save(cible, "JPEG", quality=92, optimize=True, progressive=True)  # sans EXIF : GPS supprimé
        apres = diagnostic(res)
        print(f"  -> {cible} ({apres['taille']}, luminance {apres['luminance']}, netteté {apres['nettete']})")
        if args.compare:
            planche = args.out / f"{chemin.stem}-avant-apres.jpg"
            avant_apres(src, res).save(planche, "JPEG", quality=88)
            print(f"  -> {planche}")
    sys.exit(code)


if __name__ == "__main__":
    main()
