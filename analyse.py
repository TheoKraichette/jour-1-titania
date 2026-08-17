"""Bureau d'Analyse Terrestre — relevés de la sonde Klaxo-3.

Télécharge la transmission et rejoue les six phases d'une traite.

    python analyse.py
"""

import csv
import os
import re
import urllib.request
from collections import Counter

import matplotlib
import polars as pl

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

URL = (
    "https://raw.githubusercontent.com/planetsig/ufo-reports/master/csv-data/"
    "ufo-complete-geocoded-time-standardized.csv"
)
CSV = "releves_klaxo3.csv"

# Le fichier est livré sans en-têtes.
COLONNES = [
    "datetime",
    "city",
    "state",
    "country",
    "shape",
    "duration_seconds",
    "duration_hours_min",
    "comments",
    "date_posted",
    "latitude",
    "longitude",
]


def titre(n, nom):
    print(f"\n{'=' * 70}\nPHASE {n} — {nom}\n{'=' * 70}")


def recuperer_la_transmission():
    if os.path.exists(CSV):
        print(f"{CSV} déjà présent ({os.path.getsize(CSV) / 1e6:.1f} Mo)")
        return
    print(f"Téléchargement depuis {URL}")
    urllib.request.urlretrieve(URL, CSV)
    print(f"Reçu : {os.path.getsize(CSV) / 1e6:.1f} Mo")


def compter_lignes_physiques(chemin):
    with open(chemin, "rb") as f:
        return sum(bloc.count(b"\n") for bloc in iter(lambda: f.read(1 << 20), b""))


DATE_SEULE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")


def _numerique(valeur):
    try:
        float(valeur)
        return True
    except ValueError:
        return False


def reconstructions(champs):
    """Reconstructions distinctes d'une ligne trop longue, en retirant un champ vide.

    Une reconstruction n'est retenue que si les champs repères retombent sur leurs
    pieds : date de publication, coordonnées et durée.
    """
    trouvees = set()
    for i, valeur in enumerate(champs):
        if valeur != "":
            continue
        candidat = tuple(champs[:i] + champs[i + 1 :])
        if len(candidat) != len(COLONNES):
            continue
        if (
            DATE_SEULE.match(candidat[8])
            and _numerique(candidat[9])
            and _numerique(candidat[10])
            and (candidat[5] == "" or _numerique(candidat[5]))
        ):
            trouvees.add(candidat)
    return trouvees


def phase1_ouvrir_la_caisse():
    """Charger tout le fichier. Rendre : lignes du fichier, chargées, mises à part."""
    titre(1, "ouvrir la caisse")

    lignes_physiques = compter_lignes_physiques(CSV)

    # Parcours manuel plutôt qu'un read_csv tolérant : on veut garder les lignes
    # rejetées sous la main, pas les laisser disparaître.
    conformes, rejets = [], []
    with open(CSV, encoding="utf-8", errors="replace", newline="") as f:
        for numero, champs in enumerate(csv.reader(f), start=1):
            if len(champs) == len(COLONNES):
                conformes.append(champs)
            else:
                rejets.append((numero, champs))

    total = len(conformes) + len(rejets)
    df = pl.DataFrame(
        conformes, schema={col: pl.String for col in COLONNES}, orient="row"
    )

    print(f"Lignes physiques (\\n)      : {lignes_physiques}")
    print(f"Enregistrements CSV        : {total}")
    print(f"Chargés (11 champs)        : {df.height}")
    print(f"Mis à part                 : {len(rejets)}")
    print(f"Cohérence                  : {df.height} + {len(rejets)} = {total}")

    if rejets:
        print("\nCe qu'il y a dans les lignes mises à part :")
        for nb_champs, combien in sorted(Counter(len(c) for _, c in rejets).items()):
            print(f"  {combien} ligne(s) à {nb_champs} champs au lieu de {len(COLONNES)}")

        numero, champs = rejets[0]
        print(f"\nExemple, ligne {numero} ({len(champs)} champs) :")
        for i, valeur in enumerate(champs):
            nom = COLONNES[i] if i < len(COLONNES) else "— en trop —"
            print(f"  [{i:2}] {nom:<19} {valeur[:60]}")

        # Peut-on les recoller en retirant le champ vide en trop ?
        combien = Counter(len(reconstructions(c)) for _, c in rejets)
        uniques = combien[1]
        ambigues = sum(n for k, n in combien.items() if k > 1)
        print(
            f"\nRecollage : {uniques} lignes ont une reconstruction unique, "
            f"{ambigues} en ont plusieurs, {combien[0]} aucune."
        )

    return df


NUMERIQUES = ["duration_seconds", "latitude", "longitude"]


def phase2_typer(df):
    """Chaque champ dans son vrai type, sans supprimer de ligne."""
    titre(2, "rien n'est du bon type")

    conversions = {col: pl.col(col).cast(pl.Float64, strict=False) for col in NUMERIQUES}
    conversions["datetime"] = pl.col("datetime").str.to_datetime(
        "%m/%d/%Y %H:%M", strict=False
    )
    # date_posted n'a pas d'heure : une vraie date, pas un datetime à minuit.
    conversions["date_posted"] = pl.col("date_posted").str.to_date(
        "%m/%d/%Y", strict=False
    )

    print(f"{'champ':<18} {'vides':>6} {'résistent':>10}   valeurs fautives")
    for col, expr in conversions.items():
        brut = df[col]
        converti = df.select(expr).to_series()
        vide = brut.str.strip_chars() == ""
        resiste = ~vide & converti.is_null()
        fautives = brut.filter(resiste).unique().head(4).to_list()
        print(f"{col:<18} {vide.sum():>6} {resiste.sum():>10}   {fautives}")

    duree = df["duration_seconds"]
    natures = {
        "lettre parasite dans latitude": df["latitude"].str.contains("[A-Za-z]"),
        "apostrophe inversée collée à la durée": duree.str.contains("`"),
        "espaces autour de la durée": (duree != duree.str.strip_chars()) & (duree != ""),
        "heure 24:00, qui n'existe pas": df["datetime"].str.contains(" 24:"),
        "entités HTML dans le témoignage": df["comments"].str.contains("&#"),
        "pays non renseigné": df["country"].str.strip_chars() == "",
    }
    # Celle-ci passe la conversion sans broncher : zéro est un nombre valide.
    lat = df.select(conversions["latitude"]).to_series()
    lon = df.select(conversions["longitude"]).to_series()
    natures["coordonnées à (0, 0), géocodage raté"] = (lat == 0) & (lon == 0)
    print("\nAnomalies par nature :")
    for nom, masque in natures.items():
        print(f"  {nom:<40} {masque.sum():>6}")

    avant = df.height
    df = df.with_columns(**conversions)
    print(f"\nLignes : {avant} en entrée, {df.height} en sortie.")
    print("Type final de chaque champ :")
    for nom, type_final in df.schema.items():
        print(f"  {nom:<20} {type_final}")

    carte_des_observations(df)
    return df


def carte_des_observations(df):
    """La carte que le Conseil n'arrivait pas à tracer, une fois les types corrigés."""
    points = df.select("latitude", "longitude").drop_nulls()
    os.makedirs("figures", exist_ok=True)

    fig, ax = plt.subplots(figsize=(13, 6.5))
    ax.scatter(points["longitude"], points["latitude"], s=1, alpha=0.15, linewidths=0)
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    ax.set_title(f"{points.height} observations situées")
    fig.savefig("figures/carte.png", dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"\nCarte : figures/carte.png ({points.height} points)")


def phase3_etiqueter_les_canulars(df):
    """Fabriquer l'étiquette « canular » : aucun champ ne la donne."""
    titre(3, "trier les canulars")

    bas = pl.col("comments").str.to_lowercase()
    # Le Bureau annote les dossiers douteux entre doubles parenthèses : ((HOAX??)),
    # ((NUFORC Note: Possible hoax?? PD)) ou ((NUFORC Note: Student report. PD)).
    dans_note = bas.str.contains(r"\(\([^)]*(?:hoax|student report)")
    df = df.with_columns(canular=dans_note)

    marques = int(df["canular"].sum())
    print("Règle : dans sa note, le Bureau parle de « hoax » ou de « student report ».")
    print(f"Marqués canulars : {marques} / {df.height} ({marques / df.height:.2%})")

    compte = lambda expr: int(df.select(expr).to_series().sum())  # noqa: E731
    hoax = bas.str.contains(r"\(\([^)]*hoax")
    print(f"  dont « hoax »                                    : {compte(hoax)}")
    print(f"  dont « student report » seul                     : {compte(dans_note & ~hoax)}")
    print(f"  « hoax » écrit par le témoin, hors note, écarté  : "
          f"{compte(bas.str.contains('hoax', literal=True) & ~hoax)}")
    print(f"  parmi les marqués, « hoax?? », donc un doute     : "
          f"{compte(bas.str.contains('hoax??', literal=True) & dans_note)}")

    # Une méprise n'est pas un canular : le témoin a vu quelque chose, il l'a mal
    # identifié. Ces notes-là ne comptent pas.
    meprises = bas.str.contains(
        r"\(\([^)]*(?:venus|sirius|jupiter|contrail|satellite|meteor|missile"
        r"|advertising|balloon)"
    )
    print(f"  méprises annotées par le Bureau, non comptées    : {compte(meprises)}")

    print("\nExemples marqués :")
    for texte in df.filter(pl.col("canular"))["comments"].head(3).to_list():
        print("  -", texte[:100].replace("&#44", ","))

    return df


def phase4_premier_verdict(df):
    """Un modèle, évalué sur des relevés jamais vus : rappel et précision."""
    titre(4, "le premier verdict")
    # TODO
    return None


def phase5_fuite_temporelle(df):
    """Retirer les colonnes remplies après coup, réentraîner, comparer."""
    titre(5, "le Conseil ne vous croit pas")
    # TODO : tableau qui écrit quoi et quand, puis les deux nombres avant / après.
    return None


def phase6_modele_du_stagiaire(df):
    """Baseline « jamais un canular », à mettre en face du vrai modèle."""
    titre(6, "le modèle le plus bête du Bureau")
    # TODO
    return None


def main():
    recuperer_la_transmission()
    df = phase1_ouvrir_la_caisse()
    df = phase2_typer(df)
    df = phase3_etiqueter_les_canulars(df)
    phase4_premier_verdict(df)
    phase5_fuite_temporelle(df)
    phase6_modele_du_stagiaire(df)
    print("\nFin de l'analyse. Les chiffres sont à reporter dans RAPPORT.md.")


if __name__ == "__main__":
    main()
