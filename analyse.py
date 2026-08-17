"""Bureau d'Analyse Terrestre — relevés de la sonde Klaxo-3.

Télécharge la transmission et rejoue les six phases d'une traite.

    python analyse.py
"""

import csv
import os
import urllib.request
from collections import Counter

import polars as pl

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

    return df


def phase2_typer(df):
    """Chaque champ dans son vrai type, sans supprimer de ligne."""
    titre(2, "rien n'est du bon type")
    # TODO : convertir, puis compter et montrer les valeurs qui ont résisté.
    # Quatre anomalies de nature différente au minimum, avec leur origine.
    return df


def phase3_etiqueter_les_canulars(df):
    """Fabriquer l'étiquette « canular » : aucun champ ne la donne."""
    titre(3, "trier les canulars")
    # TODO : une règle tenant en une phrase, son compte, sa proportion, sa limite.
    # La source de l'étiquette conditionne la phase 5.
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
